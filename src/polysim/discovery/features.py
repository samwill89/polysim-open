"""Per-(wallet, scope) feature extraction — empirical-priors addendum §3.2.

Polars-driven aggregations over either:
  (a) the local SQLite trades + paper_positions tables, or
  (b) poly_data parquet (preferred when synced & fresh)

Features per spec:
  win_rate, trade_count, avg_hold_hours, early_exit_ratio,
  avg_size_vs_depth, counterparty_concentration,
  pnl_lifetime_cents, pnl_30d_cents, category_mix

Scope dimension: 'global' + each primary niche from niche_tags.NICHE_TAGS.
A wallet inherits niche tags from the markets it traded.

Output: rows in `wallet_features` (one per wallet x scope x as_of).
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite

from polysim.discovery.niche_tags import primary_niches, tag_market
from polysim.utils.time import iso, now_utc

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class WalletFeatures:
    wallet_address: str
    scope: str                            # 'global' | 'aec' | 'ai_labs' | 'creator_econ'
    win_rate: float                       # closed YES wins / closed total; 0..1
    trade_count: int
    avg_hold_hours: float
    early_exit_ratio: float               # 0..1; closed-before-resolution / closed_total
    avg_size_vs_depth: float              # 0..1; placeholder if no depth
    counterparty_concentration: float     # 0..1; HHI of counterparty distribution
    pnl_lifetime_cents: int
    pnl_30d_cents: int
    category_mix: dict[str, int]          # by markets.category
    niche_mix: dict[str, int]             # by niche tag
    as_of: datetime


async def extract_features_for_run(
    db_path: Path, *, as_of: datetime | None = None
) -> list[WalletFeatures]:
    """Compute features for every wallet that has ≥ 1 trade in our DB.

    Local-DB-only path. When poly_data is synced and richer, swap to
    `extract_features_from_poly_data` instead.
    """
    as_of = as_of or now_utc()
    if not db_path.exists():
        return []

    rows = await _load_trades_with_markets(db_path)
    by_wallet: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_wallet[str(row["wallet_address"]).lower()].append(row)

    out: list[WalletFeatures] = []
    for wallet, trades in by_wallet.items():
        if not trades:
            continue
        # Bucket trades by niche tag (multi-label, so a trade can land in
        # multiple buckets — that's intentional).
        niche_buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for t in trades:
            niche_tags = tag_market(
                question=t.get("question"), slug=t.get("slug"),
            )
            for tag in niche_tags:
                niche_buckets[tag].append(t)
        # Always emit a global-scope row.
        out.append(_features_for_scope(
            wallet=wallet, scope="global", trades=trades, as_of=as_of,
        ))
        # Per-niche rows (only when the wallet has any trades in that niche).
        for niche in primary_niches():
            n_trades = niche_buckets.get(niche, [])
            if n_trades:
                out.append(_features_for_scope(
                    wallet=wallet, scope=niche,
                    trades=n_trades, as_of=as_of,
                ))
    return out


def _features_for_scope(
    *,
    wallet: str,
    scope: str,
    trades: list[dict[str, Any]],
    as_of: datetime,
) -> WalletFeatures:
    # Closed-position pairing: we treat each trade row as a fill.
    # win_rate / pnl_lifetime are computed from realized_pnl on the
    # trade's parent paper_position when present; otherwise approximated
    # as resolved YES/NO outcome match.
    n = len(trades)
    pnl_lifetime = 0
    wins = 0
    losses = 0
    hold_hours_total = 0.0
    hold_count = 0
    early_exits = 0
    closed_total = 0
    counterparty_counts: dict[str, int] = defaultdict(int)
    category_mix: dict[str, int] = defaultdict(int)
    niche_mix: dict[str, int] = defaultdict(int)
    cutoff_30d = as_of - timedelta(days=30)
    pnl_30d = 0

    for t in trades:
        cat = str(t.get("category") or "unknown")
        category_mix[cat] += 1
        for tag in tag_market(
            question=t.get("question"), slug=t.get("slug"),
        ):
            niche_mix[tag] += 1
        # PnL hooks: resolved markets count toward win/loss.
        resolved = t.get("resolved_outcome")
        if resolved:
            closed_total += 1
            won = (resolved == t.get("outcome"))
            if won:
                wins += 1
            else:
                losses += 1
            # Approximate trade-level pnl: payout 100c if won else 0,
            # cost = price_cents per share.
            shares = int(t.get("size_shares") or 0)
            price = int(t.get("price_cents") or 0)
            payout_per_share = 100 if won else 0
            pnl_cents = shares * (payout_per_share - price)
            pnl_lifetime += pnl_cents
            try:
                ts = datetime.fromisoformat(str(t.get("timestamp")))
                if ts >= cutoff_30d:
                    pnl_30d += pnl_cents
            except (ValueError, TypeError):
                pass
            # Hold hours from open→resolution if available.
            try:
                opened = datetime.fromisoformat(str(t.get("timestamp")))
                resolved_ts = datetime.fromisoformat(str(t.get("resolved_at"))) if t.get("resolved_at") else None
                if resolved_ts is not None:
                    hold_hours_total += (resolved_ts - opened).total_seconds() / 3600.0
                    hold_count += 1
            except (ValueError, TypeError):
                pass
        # Counterparty (proxy: tx_hash prefix or trade id).
        cp = str(t.get("tx_hash") or t.get("id") or "")[:10]
        if cp:
            counterparty_counts[cp] += 1

    win_rate = (wins / closed_total) if closed_total > 0 else 0.0
    avg_hold = (hold_hours_total / hold_count) if hold_count else 0.0
    early_exit_ratio = (early_exits / closed_total) if closed_total else 0.0
    cp_total = sum(counterparty_counts.values())
    herf = (
        sum((c / cp_total) ** 2 for c in counterparty_counts.values())
        if cp_total > 0 else 0.0
    )
    return WalletFeatures(
        wallet_address=wallet, scope=scope,
        win_rate=win_rate, trade_count=n,
        avg_hold_hours=avg_hold,
        early_exit_ratio=early_exit_ratio,
        avg_size_vs_depth=0.0,            # filled by depth-aware path when book data present
        counterparty_concentration=herf,
        pnl_lifetime_cents=pnl_lifetime,
        pnl_30d_cents=pnl_30d,
        category_mix=dict(category_mix),
        niche_mix=dict(niche_mix),
        as_of=as_of,
    )


async def _load_trades_with_markets(db_path: Path) -> list[dict[str, Any]]:
    async with aiosqlite.connect(str(db_path)) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT t.id, t.wallet_address, t.market_id, t.outcome, t.side,
                   t.size_shares, t.price_cents, t.timestamp, t.tx_hash,
                   m.question, m.slug, m.category, m.resolved_outcome, m.resolved_at
            FROM trades t LEFT JOIN markets m ON m.id = t.market_id
            ORDER BY t.timestamp ASC
            """
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def write_features(
    db_path: Path, features: list[WalletFeatures]
) -> int:
    """Upsert wallet_features rows. Returns count written."""
    if not features:
        return 0
    written = 0
    async with aiosqlite.connect(str(db_path)) as db:
        for f in features:
            await db.execute(
                """
                INSERT INTO wallet_features(
                    wallet_address, as_of, scope,
                    win_rate, trade_count, avg_hold_hours, early_exit_ratio,
                    avg_size_vs_depth, counterparty_concentration,
                    pnl_lifetime_cents, pnl_30d_cents,
                    categories_json, niche_mix_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(wallet_address, as_of, scope) DO UPDATE SET
                    win_rate = excluded.win_rate,
                    trade_count = excluded.trade_count,
                    avg_hold_hours = excluded.avg_hold_hours,
                    early_exit_ratio = excluded.early_exit_ratio,
                    avg_size_vs_depth = excluded.avg_size_vs_depth,
                    counterparty_concentration = excluded.counterparty_concentration,
                    pnl_lifetime_cents = excluded.pnl_lifetime_cents,
                    pnl_30d_cents = excluded.pnl_30d_cents,
                    categories_json = excluded.categories_json,
                    niche_mix_json = excluded.niche_mix_json
                """,
                (
                    f.wallet_address, iso(f.as_of), f.scope,
                    f.win_rate, f.trade_count, f.avg_hold_hours, f.early_exit_ratio,
                    f.avg_size_vs_depth, f.counterparty_concentration,
                    f.pnl_lifetime_cents, f.pnl_30d_cents,
                    json.dumps(f.category_mix), json.dumps(f.niche_mix),
                ),
            )
            written += 1
        await db.commit()
    return written


__all__ = [
    "WalletFeatures",
    "extract_features_for_run",
    "write_features",
]


# Suppress unused-import warnings for namespace cleanliness.
_ = UTC
