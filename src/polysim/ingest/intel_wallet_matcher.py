"""Close the loop: sentiment → candidate insider wallets.

For each intel_interpretation with a matched_market_id + direction + high
conviction, find the wallets whose recent trades on that market in that
direction best match "insider signature": large, contrarian, fresh.

Every candidate is promoted to `known_insiders` with source
"sentiment-match:<source>:<msg_id>" so downstream detectors can give
these wallets a confidence boost when they next trade.

Design decisions:
  * Window: [post_time - match_window_before_h, post_time + match_window_after_h].
    Default 48h before / 24h after — insider moves usually precede the post.
  * Rank by size_shares (cents notional) in the matching direction.
  * Cap to top-N per interpretation to avoid polluting known_insiders.
  * Never touches the `flags` or `paper_positions` tables — feed-only.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any

import aiosqlite

from polysim.db import dao
from polysim.utils.time import parse_iso

log = logging.getLogger(__name__)


async def _backfill_trades_for_market(
    db_path: Path, *, market_id: str, max_pages: int = 4, page_size: int = 500
) -> int:
    """On-demand fetch of historical trades for a single market from data-api.

    Our live poller only catches recent trades. For sentiment-matched
    historical markets we need to backfill. Idempotent (trades.id unique).
    """
    from polysim.config import load_config
    from polysim.ingest.polymarket_rest import PolymarketREST

    try:
        cfg = load_config()
    except Exception:
        return 0
    ingested = 0
    try:
        async with PolymarketREST(
            gamma_base_url=cfg.ingest.polymarket_gamma_url,
            data_base_url=cfg.ingest.polymarket_data_url,
            clob_base_url=cfg.ingest.polymarket_clob_url,
        ) as rest:
            for page in range(max_pages):
                trades = await rest.get_trades(
                    market_id=market_id, limit=page_size, offset=page * page_size,
                )
                if not trades:
                    break
                ingested += await dao.insert_trades_batch(db_path, trades)
                if len(trades) < page_size:
                    break
    except Exception as exc:
        log.warning("backfill trades for %s failed: %s", market_id[:10], exc)
    return ingested


async def candidate_wallets_for_interpretation(
    db_path: Path,
    *,
    market_id: str,
    direction: str,                          # "YES" | "NO"
    post_time_iso: str,
    match_window_before_h: float = 48.0,
    match_window_after_h: float = 24.0,
    min_notional_cents: int = 10_000,        # $100 minimum to be interesting
    top_n: int = 10,
    backfill_if_empty: bool = True,
) -> list[dict[str, Any]]:
    """Return top-N wallets trading `market_id` in `direction` within the
    time window around `post_time_iso`. Ranked by total notional.

    Results carry (wallet_address, total_notional_cents, trade_count,
    max_single_notional_cents, earliest_ts). "Earliest" is the most
    actionable — that's the wallet who moved first.
    """
    if not db_path.exists():
        return []
    post_time = parse_iso(post_time_iso)
    t_start = post_time - timedelta(hours=match_window_before_h)
    t_end = post_time + timedelta(hours=match_window_after_h)

    # If we have no trades at all for this market, do a one-shot backfill.
    if backfill_if_empty:
        async with aiosqlite.connect(str(db_path)) as check_db, check_db.execute(
            "SELECT 1 FROM trades WHERE market_id = ? LIMIT 1", (market_id,)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            n = await _backfill_trades_for_market(db_path, market_id=market_id)
            if n:
                log.info("backfilled %d trades for %s", n, market_id[:10])

    try:
        async with aiosqlite.connect(str(db_path)) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT wallet_address,
                       COUNT(*) AS trade_count,
                       SUM(size_shares * price_cents) AS total_notional,
                       MAX(size_shares * price_cents) AS max_single,
                       MIN(timestamp) AS earliest_ts,
                       MAX(timestamp) AS latest_ts
                FROM trades
                WHERE market_id = ?
                  AND outcome = ?
                  AND side = 'BUY'
                  AND timestamp BETWEEN ? AND ?
                GROUP BY wallet_address
                HAVING total_notional >= ?
                ORDER BY total_notional DESC
                LIMIT ?
                """,
                (
                    market_id, direction,
                    t_start.isoformat(), t_end.isoformat(),
                    min_notional_cents, top_n,
                ),
            ) as cur:
                rows = list(await cur.fetchall())
    except aiosqlite.OperationalError:
        return []

    out: list[dict[str, Any]] = []
    for r in rows:
        out.append({
            "wallet_address": r["wallet_address"],
            "trade_count": int(r["trade_count"] or 0),
            "total_notional_cents": int(r["total_notional"] or 0),
            "max_single_notional_cents": int(r["max_single"] or 0),
            "earliest_ts": r["earliest_ts"],
            "latest_ts": r["latest_ts"],
        })
    return out


async def match_wallets_for_source(
    db_path: Path,
    *,
    source: str | None = None,
    min_conviction: float = 0.55,
    match_window_before_h: float = 48.0,
    match_window_after_h: float = 24.0,
    min_notional_cents: int = 10_000,
    top_n_per_post: int = 10,
    max_interpretations: int = 500,
) -> dict[str, int]:
    """Scan interpretations, find candidate wallets per match, upsert into
    known_insiders with sentiment-match provenance.

    Returns counters: {"interpretations_scanned", "interpretations_matched",
    "wallets_promoted", "wallets_skipped_dup"}.
    """
    query = (
        "SELECT i.*, m.source, m.posted_at, m.external_id "
        "FROM intel_interpretations i "
        "JOIN intel_messages m ON m.id = i.intel_message_id "
        "WHERE i.matched_market_id IS NOT NULL "
        "  AND i.direction IS NOT NULL "
        "  AND i.is_market_relevant = 1 "
        "  AND i.conviction >= ? "
    )
    args: list[Any] = [min_conviction]
    if source is not None:
        query += "AND m.source = ? "
        args.append(source)
    query += "ORDER BY m.posted_at DESC LIMIT ?"
    args.append(max_interpretations)

    if not db_path.exists():
        return {
            "interpretations_scanned": 0, "interpretations_matched": 0,
            "wallets_promoted": 0, "wallets_skipped_dup": 0,
        }
    try:
        async with aiosqlite.connect(str(db_path)) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(query, args) as cur:
                rows = [dict(r) for r in await cur.fetchall()]
    except aiosqlite.OperationalError:
        return {
            "interpretations_scanned": 0, "interpretations_matched": 0,
            "wallets_promoted": 0, "wallets_skipped_dup": 0,
        }

    matched_posts = 0
    promoted = 0
    skipped_dup = 0
    for r in rows:
        mid = str(r.get("matched_market_id") or "")
        direction = str(r.get("direction") or "")
        post_ts = str(r.get("posted_at") or "")
        if not (mid and direction and post_ts):
            continue
        candidates = await candidate_wallets_for_interpretation(
            db_path,
            market_id=mid, direction=direction, post_time_iso=post_ts,
            match_window_before_h=match_window_before_h,
            match_window_after_h=match_window_after_h,
            min_notional_cents=min_notional_cents,
            top_n=top_n_per_post,
        )
        if not candidates:
            continue
        matched_posts += 1
        src_name = str(r.get("source") or "unknown")
        msg_id = int(r.get("intel_message_id") or 0)
        external_id = str(r.get("external_id") or "?")
        summary = str(r.get("summary") or "")[:160]
        for c in candidates:
            w = str(c["wallet_address"] or "").lower()
            if not w:
                continue
            # Label format: <source>:sent:<msg_id>:<wallet_short>.
            label = f"{src_name}:sent:{external_id}:{w[:10]}"
            existing = await _known_insider_exists(db_path, label=label)
            if existing:
                skipped_dup += 1
                continue
            notional = int(c["total_notional_cents"] or 0)
            tc = int(c["trade_count"] or 0)
            posted_at_str = str(r.get("posted_at") or "")[:10]
            notes = (
                f"@{src_name} msg {external_id}: {summary}"
                + f" | matched {tc} trade(s) totalling ${notional/100:,.0f} "
                + f"on {mid[:10]}... direction {direction}"
                + f" between {posted_at_str} ±{int(match_window_before_h)}h"
            )
            await dao.upsert_known_insider(
                db_path,
                label=label,
                address=w,
                source=f"sentiment-match:{src_name}",
                source_message_id=msg_id,
                notes=notes,
            )
            promoted += 1

    return {
        "interpretations_scanned": len(rows),
        "interpretations_matched": matched_posts,
        "wallets_promoted": promoted,
        "wallets_skipped_dup": skipped_dup,
    }


async def _known_insider_exists(db_path: Path, *, label: str) -> bool:
    try:
        async with aiosqlite.connect(str(db_path)) as db, db.execute(
            "SELECT 1 FROM known_insiders WHERE label = ? LIMIT 1",
            (label,),
        ) as cur:
            row = await cur.fetchone()
    except aiosqlite.OperationalError:
        return False
    return row is not None


# Keep imports referenced for linting.
_ = (Sequence, json, log)


__all__ = [
    "candidate_wallets_for_interpretation",
    "match_wallets_for_source",
]
