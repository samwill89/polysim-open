"""Async data-access layer. Thin, typed wrapper around aiosqlite.

Build plan §1.5 — upsert semantics on first-sight wallet; bulk insert of
trades (idempotent via UNIQUE id); market category update; clock-skew
samples (G3).

Every function takes a db_path so callers needn't thread a connection.
The context manager is short-lived per call — fine for our write rates
(single-digit writes/sec expected from the WS firehose).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

import aiosqlite

from polysim.models import Flag, Market, TradeEvent, Wallet, WalletProfile
from polysim.utils.time import iso, now_utc, parse_iso

log = logging.getLogger(__name__)

# ── status / stats ─────────────────────────────────────────


async def db_stats(db_path: Path) -> dict[str, int]:
    out: dict[str, int] = {"trades": 0, "wallets": 0, "markets": 0, "flags": 0}
    if not db_path.exists():
        return out
    async with aiosqlite.connect(str(db_path)) as db:
        for table in out:
            try:
                async with db.execute(f"SELECT COUNT(*) FROM {table}") as cur:
                    row = await cur.fetchone()
                    if row is not None and row[0] is not None:
                        out[table] = int(row[0])
            except aiosqlite.OperationalError:
                continue
    return out


async def list_active_runs(db_path: Path) -> list[dict[str, Any]]:
    if not db_path.exists():
        return []
    try:
        async with aiosqlite.connect(str(db_path)) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT id, name, started_at, current_balance_cents "
                "FROM paper_runs WHERE ended_at IS NULL ORDER BY started_at DESC"
            ) as cur:
                rows = await cur.fetchall()
        return [dict(r) for r in rows]
    except aiosqlite.OperationalError:
        return []


# ── markets ───────────────────────────────────────────────


async def upsert_market(db_path: Path, market: Market) -> None:
    async with aiosqlite.connect(str(db_path)) as db:
        await _upsert_market(db, market)
        await db.commit()


async def upsert_markets(db_path: Path, markets: Iterable[Market]) -> int:
    markets = list(markets)
    if not markets:
        return 0
    async with aiosqlite.connect(str(db_path)) as db:
        for m in markets:
            await _upsert_market(db, m)
        await db.commit()
    return len(markets)


async def _upsert_market(db: aiosqlite.Connection, m: Market) -> None:
    await db.execute(
        """
        INSERT INTO markets(
            id, slug, question, category, created_at,
            resolves_at, resolved_outcome, resolved_at,
            daily_volume_usd_cents, metadata_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            slug = excluded.slug,
            question = excluded.question,
            category = COALESCE(markets.category, excluded.category),
            resolves_at = excluded.resolves_at,
            resolved_outcome = COALESCE(excluded.resolved_outcome, markets.resolved_outcome),
            resolved_at = COALESCE(excluded.resolved_at, markets.resolved_at),
            daily_volume_usd_cents = excluded.daily_volume_usd_cents,
            metadata_json = excluded.metadata_json
        """,
        (
            m.id,
            m.slug,
            m.question,
            m.category,
            iso(m.created_at),
            iso(m.resolves_at) if m.resolves_at else None,
            m.resolved_outcome,
            iso(m.resolved_at) if m.resolved_at else None,
            m.daily_volume_usd_cents,
            json.dumps(m.metadata, default=str) if m.metadata else None,
        ),
    )


async def update_market_category(
    db_path: Path, market_id: str, category: str
) -> None:
    async with aiosqlite.connect(str(db_path)) as db:
        await db.execute(
            "UPDATE markets SET category = ? WHERE id = ?",
            (category, market_id),
        )
        await db.commit()


async def get_markets_missing_category(
    db_path: Path, limit: int = 100
) -> list[dict[str, Any]]:
    async with aiosqlite.connect(str(db_path)) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, question FROM markets WHERE category IS NULL LIMIT ?",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


# ── wallets ───────────────────────────────────────────────


async def upsert_wallet_first_sight(
    db_path: Path,
    address: str,
    *,
    display_name: str | None = None,
) -> bool:
    """Create a wallet row on first sighting; do nothing if already known.

    Returns True if a new row was inserted (caller should enqueue for
    Polygon enrichment), False if the wallet was already tracked.
    """
    async with aiosqlite.connect(str(db_path)) as db:
        cur = await db.execute(
            """
            INSERT INTO wallets(address, first_seen_at, display_name)
            VALUES (?, ?, ?)
            ON CONFLICT(address) DO NOTHING
            """,
            (address.lower(), iso(now_utc()), display_name),
        )
        inserted = (cur.rowcount or 0) > 0
        await cur.close()
        await db.commit()
    return inserted


async def upsert_wallet_enrichment(
    db_path: Path,
    *,
    address: str,
    nonce: int,
    funding_source: str | None,
    funding_first_deposit_at: str | None,
    owner_address: str | None = None,
) -> None:
    async with aiosqlite.connect(str(db_path)) as db:
        await db.execute(
            """
            UPDATE wallets SET
                nonce = ?,
                funding_source = ?,
                funding_first_deposit_at = ?,
                owner_address = COALESCE(?, owner_address)
            WHERE address = ?
            """,
            (
                nonce,
                funding_source,
                funding_first_deposit_at,
                owner_address.lower() if owner_address else None,
                address.lower(),
            ),
        )
        await db.commit()


async def list_new_wallets(
    db_path: Path, limit: int = 200
) -> list[Wallet]:
    """Return wallets missing nonce data — candidates for enrichment."""
    async with aiosqlite.connect(str(db_path)) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT address, first_seen_at, nonce, funding_source,
                   funding_first_deposit_at, lifetime_volume_cents,
                   lifetime_trades, display_name, owner_address,
                   metadata_json, last_profiled_at
            FROM wallets
            WHERE nonce IS NULL
            LIMIT ?
            """,
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
    out: list[Wallet] = []
    for r in rows:
        out.append(
            Wallet(
                address=r["address"],
                first_seen_at=r["first_seen_at"],
                nonce=r["nonce"],
                funding_source=r["funding_source"],
                funding_first_deposit_at=r["funding_first_deposit_at"],
                lifetime_volume_cents=int(r["lifetime_volume_cents"] or 0),
                lifetime_trades=int(r["lifetime_trades"] or 0),
                display_name=r["display_name"],
                owner_address=r["owner_address"],
                metadata=json.loads(r["metadata_json"]) if r["metadata_json"] else {},
                last_profiled_at=r["last_profiled_at"],
            )
        )
    return out


# ── trades ────────────────────────────────────────────────


async def insert_trades_batch(
    db_path: Path, trades: Sequence[TradeEvent]
) -> int:
    """Idempotent bulk insert (ON CONFLICT DO NOTHING on trades.id).

    Also upserts markets.id + wallets.address rows to satisfy foreign keys
    — needed when a trade arrives before the market/wallet is known.
    Returns count of newly inserted trade rows.
    """
    if not trades:
        return 0
    async with aiosqlite.connect(str(db_path)) as db:
        # Placeholder markets/wallets to satisfy FKs. Real metadata gets
        # filled in by the market indexer / enrichment worker later.
        for t in trades:
            await db.execute(
                "INSERT OR IGNORE INTO markets(id, slug, question, created_at) "
                "VALUES (?, ?, ?, ?)",
                (t.market_id, f"unknown-{t.market_id[:12]}", "(pending)", iso(now_utc())),
            )
            await db.execute(
                "INSERT OR IGNORE INTO wallets(address, first_seen_at) VALUES (?, ?)",
                (t.wallet_address.lower(), iso(now_utc())),
            )
        # Bulk trade insert.
        inserted = 0
        for t in trades:
            cur = await db.execute(
                """
                INSERT OR IGNORE INTO trades(
                    id, wallet_address, market_id, side, outcome,
                    size_shares, price_cents, timestamp, tx_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    t.id,
                    t.wallet_address.lower(),
                    t.market_id,
                    t.side,
                    t.outcome,
                    t.size_shares,
                    t.price_cents,
                    iso(t.timestamp),
                    t.tx_hash,
                ),
            )
            if (cur.rowcount or 0) > 0:
                inserted += 1
            await cur.close()

        # Update lifetime_volume + lifetime_trades after batch.
        wallet_deltas: dict[str, tuple[int, int]] = {}
        for t in trades:
            w = t.wallet_address.lower()
            trades_add, vol_add = wallet_deltas.get(w, (0, 0))
            wallet_deltas[w] = (
                trades_add + 1,
                vol_add + (t.size_shares * t.price_cents),
            )
        for w, (ntr, nvol) in wallet_deltas.items():
            await db.execute(
                "UPDATE wallets "
                "SET lifetime_trades = lifetime_trades + ?, "
                "    lifetime_volume_cents = lifetime_volume_cents + ? "
                "WHERE address = ?",
                (ntr, nvol, w),
            )

        await db.commit()
    return inserted


# ── clock skew (G3) ───────────────────────────────────────


async def record_clock_skews(
    db_path: Path, skews_ms: Iterable[int], source: str = "polymarket_ws"
) -> int:
    """Persist a batch of clock-skew samples. Returns count inserted."""
    skews = list(skews_ms)
    if not skews:
        return 0
    ts = iso(now_utc())
    async with aiosqlite.connect(str(db_path)) as db:
        await db.executemany(
            "INSERT INTO clock_skew_samples(measured_at, skew_ms, source) VALUES (?, ?, ?)",
            [(ts, int(s), source) for s in skews],
        )
        await db.commit()
    return len(skews)


# ── trades + markets reads (for profiler & detectors) ────


async def get_market(db_path: Path, market_id: str) -> Market | None:
    async with aiosqlite.connect(str(db_path)) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, slug, question, category, created_at, resolves_at, "
            "resolved_outcome, resolved_at, daily_volume_usd_cents, metadata_json "
            "FROM markets WHERE id = ?",
            (market_id,),
        ) as cur:
            row = await cur.fetchone()
    if row is None:
        return None
    return _row_to_market(row)


async def get_wallet(db_path: Path, address: str) -> Wallet | None:
    async with aiosqlite.connect(str(db_path)) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT address, first_seen_at, nonce, funding_source, "
            "funding_first_deposit_at, lifetime_volume_cents, lifetime_trades, "
            "display_name, owner_address, metadata_json, last_profiled_at "
            "FROM wallets WHERE address = ?",
            (address.lower(),),
        ) as cur:
            row = await cur.fetchone()
    if row is None:
        return None
    return _row_to_wallet(row)


async def get_trades_by_wallet(
    db_path: Path, wallet_address: str, *, limit: int = 10_000
) -> list[TradeEvent]:
    async with aiosqlite.connect(str(db_path)) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, wallet_address, market_id, side, outcome, "
            "size_shares, price_cents, timestamp, tx_hash "
            "FROM trades WHERE wallet_address = ? "
            "ORDER BY timestamp ASC LIMIT ?",
            (wallet_address.lower(), limit),
        ) as cur:
            rows = await cur.fetchall()
    return [_row_to_trade(r) for r in rows]


async def get_trades_by_market(
    db_path: Path,
    market_id: str,
    *,
    since: str | None = None,
    until: str | None = None,
    limit: int = 10_000,
) -> list[TradeEvent]:
    query = (
        "SELECT id, wallet_address, market_id, side, outcome, "
        "size_shares, price_cents, timestamp, tx_hash "
        "FROM trades WHERE market_id = ?"
    )
    args: list[Any] = [market_id]
    if since is not None:
        query += " AND timestamp >= ?"
        args.append(since)
    if until is not None:
        query += " AND timestamp <= ?"
        args.append(until)
    query += " ORDER BY timestamp ASC LIMIT ?"
    args.append(limit)
    async with aiosqlite.connect(str(db_path)) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(query, args) as cur:
            rows = await cur.fetchall()
    return [_row_to_trade(r) for r in rows]


async def get_trades_with_market_resolution(
    db_path: Path, wallet_address: str, *, limit: int = 10_000
) -> list[tuple[TradeEvent, Market]]:
    """Return (trade, market) pairs for one wallet — used by the profiler."""
    async with aiosqlite.connect(str(db_path)) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT t.id AS t_id, t.wallet_address, t.market_id, t.side,
                   t.outcome, t.size_shares, t.price_cents, t.timestamp,
                   t.tx_hash,
                   m.slug, m.question, m.category, m.created_at AS m_created_at,
                   m.resolves_at, m.resolved_outcome, m.resolved_at,
                   m.daily_volume_usd_cents, m.metadata_json
            FROM trades t
            JOIN markets m ON m.id = t.market_id
            WHERE t.wallet_address = ?
            ORDER BY t.timestamp ASC LIMIT ?
            """,
            (wallet_address.lower(), limit),
        ) as cur:
            rows = await cur.fetchall()
    out: list[tuple[TradeEvent, Market]] = []
    for r in rows:
        trade = TradeEvent(
            id=r["t_id"],
            wallet_address=r["wallet_address"],
            market_id=r["market_id"],
            side=r["side"],
            outcome=r["outcome"],
            size_shares=int(r["size_shares"]),
            price_cents=int(r["price_cents"]),
            timestamp=parse_iso(r["timestamp"]),
            tx_hash=r["tx_hash"],
        )
        market = Market(
            id=r["market_id"],
            slug=r["slug"],
            question=r["question"],
            category=r["category"],
            created_at=parse_iso(r["m_created_at"]),
            resolves_at=parse_iso(r["resolves_at"]) if r["resolves_at"] else None,
            resolved_outcome=r["resolved_outcome"],
            resolved_at=parse_iso(r["resolved_at"]) if r["resolved_at"] else None,
            daily_volume_usd_cents=r["daily_volume_usd_cents"],
            metadata=json.loads(r["metadata_json"]) if r["metadata_json"] else {},
        )
        out.append((trade, market))
    return out


async def list_wallets_needing_profile(
    db_path: Path, *, min_trades: int = 1, staleness_seconds: int = 60, limit: int = 500
) -> list[str]:
    """Return addresses whose profile is missing or older than staleness_seconds."""
    cutoff = iso(now_utc())
    async with aiosqlite.connect(str(db_path)) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT w.address FROM wallets w
            WHERE w.lifetime_trades >= ?
              AND (
                w.last_profiled_at IS NULL
                OR (julianday(?) - julianday(w.last_profiled_at)) * 86400 > ?
              )
            ORDER BY w.last_profiled_at ASC NULLS FIRST
            LIMIT ?
            """,
            (min_trades, cutoff, staleness_seconds, limit),
        ) as cur:
            rows = await cur.fetchall()
    return [str(r["address"]) for r in rows]


async def write_wallet_profile(
    db_path: Path, profile: WalletProfile
) -> None:
    """Persist a profile snapshot and bump wallets.last_profiled_at."""
    async with aiosqlite.connect(str(db_path)) as db:
        await db.execute(
            """
            INSERT INTO wallet_profiles(
                wallet_address, as_of, total_markets, resolved_markets,
                wins, losses, win_rate, total_pnl_cents,
                categories_json, category_exclusivity,
                avg_entry_to_resolution_hours, features_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(wallet_address, as_of) DO UPDATE SET
                total_markets = excluded.total_markets,
                resolved_markets = excluded.resolved_markets,
                wins = excluded.wins,
                losses = excluded.losses,
                win_rate = excluded.win_rate,
                total_pnl_cents = excluded.total_pnl_cents,
                categories_json = excluded.categories_json,
                category_exclusivity = excluded.category_exclusivity,
                avg_entry_to_resolution_hours = excluded.avg_entry_to_resolution_hours,
                features_json = excluded.features_json
            """,
            (
                profile.wallet_address.lower(),
                iso(profile.as_of),
                profile.total_markets,
                profile.resolved_markets,
                profile.wins,
                profile.losses,
                profile.win_rate,
                profile.total_pnl_cents,
                json.dumps(profile.categories),
                profile.category_exclusivity,
                profile.avg_entry_to_resolution_hours,
                json.dumps(profile.features, default=str) if profile.features else None,
            ),
        )
        await db.execute(
            "UPDATE wallets SET last_profiled_at = ? WHERE address = ?",
            (iso(profile.as_of), profile.wallet_address.lower()),
        )
        await db.commit()


async def get_latest_profile(
    db_path: Path, wallet_address: str
) -> WalletProfile | None:
    async with aiosqlite.connect(str(db_path)) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT * FROM wallet_profiles
            WHERE wallet_address = ?
            ORDER BY as_of DESC LIMIT 1
            """,
            (wallet_address.lower(),),
        ) as cur:
            row = await cur.fetchone()
    if row is None:
        return None
    return WalletProfile(
        wallet_address=row["wallet_address"],
        as_of=parse_iso(row["as_of"]),
        total_markets=int(row["total_markets"] or 0),
        resolved_markets=int(row["resolved_markets"] or 0),
        wins=int(row["wins"] or 0),
        losses=int(row["losses"] or 0),
        win_rate=float(row["win_rate"] or 0.0),
        total_pnl_cents=int(row["total_pnl_cents"] or 0),
        categories=json.loads(row["categories_json"]) if row["categories_json"] else {},
        category_exclusivity=float(row["category_exclusivity"] or 0.0),
        avg_entry_to_resolution_hours=float(row["avg_entry_to_resolution_hours"] or 0.0),
        features=json.loads(row["features_json"]) if row["features_json"] else {},
    )


# ── flags ────────────────────────────────────────────────


async def write_flag(db_path: Path, flag: Flag) -> int | None:
    """Insert a flag row. Returns the new id, or None on UNIQUE conflict."""
    async with aiosqlite.connect(str(db_path)) as db:
        cur = await db.execute(
            """
            INSERT OR IGNORE INTO flags(
                wallet_address, market_id, trade_id, detector_name,
                raw_score, composite_score, components_json,
                investigator_verdict, investigator_reasoning,
                created_at, acted_on
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                flag.wallet_address.lower(),
                flag.market_id,
                flag.trade_id,
                flag.detector_name,
                flag.raw_score,
                flag.composite_score,
                json.dumps(flag.components, default=str),
                flag.investigator_verdict,
                flag.investigator_reasoning,
                iso(flag.created_at),
                1 if flag.acted_on else 0,
            ),
        )
        new_id = cur.lastrowid if (cur.rowcount or 0) > 0 else None
        await cur.close()
        await db.commit()
    return new_id


async def update_flag_resolution_risk(
    db_path: Path, flag_id: int, *, score: float
) -> None:
    """Empirical-priors addendum §4.3 — write resolution_risk_score on flag.

    Score in [0.0, 1.0]. The decision gate (E3) reads this back.
    """
    s = max(0.0, min(1.0, float(score)))
    async with aiosqlite.connect(str(db_path)) as db:
        await db.execute(
            "UPDATE flags SET resolution_risk_score = ? WHERE id = ?",
            (s, flag_id),
        )
        await db.commit()


async def update_flag_investigator(
    db_path: Path,
    flag_id: int,
    *,
    verdict: str,
    reasoning: str | None,
    acted_on: bool | None = None,
) -> None:
    """Write investigator output. §14 #6 — scores are immutable here."""
    args: list[Any] = [verdict, reasoning]
    sql = "UPDATE flags SET investigator_verdict = ?, investigator_reasoning = ?"
    if acted_on is not None:
        sql += ", acted_on = ?"
        args.append(1 if acted_on else 0)
    sql += " WHERE id = ?"
    args.append(flag_id)
    async with aiosqlite.connect(str(db_path)) as db:
        await db.execute(sql, args)
        await db.commit()


async def get_recent_flag(
    db_path: Path,
    wallet_address: str,
    market_id: str,
    *,
    within_seconds: int = 600,
) -> Flag | None:
    """Return a recent flag for (wallet, market) within the window, if any."""
    cutoff = iso(now_utc())
    async with aiosqlite.connect(str(db_path)) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT * FROM flags
            WHERE wallet_address = ? AND market_id = ?
              AND (julianday(?) - julianday(created_at)) * 86400 <= ?
            ORDER BY created_at DESC LIMIT 1
            """,
            (wallet_address.lower(), market_id, cutoff, within_seconds),
        ) as cur:
            row = await cur.fetchone()
    return _row_to_flag(row) if row else None


async def list_flags_since(
    db_path: Path,
    *,
    since_iso: str,
    category: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """For CLI — returns joined flag + market-category rows.

    Returns empty list on a pre-migration DB; the CLI prints a gentle
    'no flags' message rather than crash.
    """
    if not db_path.exists():
        return []
    query = (
        "SELECT f.id, f.created_at, f.wallet_address, f.market_id, "
        "f.detector_name, f.raw_score, f.composite_score, "
        "f.investigator_verdict, f.acted_on, m.category, m.question "
        "FROM flags f LEFT JOIN markets m ON m.id = f.market_id "
        "WHERE f.created_at >= ?"
    )
    args: list[Any] = [since_iso]
    if category is not None:
        query += " AND m.category = ?"
        args.append(category)
    query += " ORDER BY f.created_at DESC LIMIT ?"
    args.append(limit)
    try:
        async with aiosqlite.connect(str(db_path)) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(query, args) as cur:
                rows = await cur.fetchall()
    except aiosqlite.OperationalError:
        return []
    return [dict(r) for r in rows]


async def record_flag_cost(
    db_path: Path,
    flag_id: int,
    *,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int,
    cost_cents: int,
    latency_ms: int | None = None,
) -> None:
    """Persist a Claude API call's cost for a flag (G7)."""
    async with aiosqlite.connect(str(db_path)) as db:
        await db.execute(
            """
            INSERT INTO flag_costs(
                flag_id, model, input_tokens, output_tokens, cached_tokens,
                cost_cents, latency_ms, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(flag_id) DO UPDATE SET
                model = excluded.model,
                input_tokens = flag_costs.input_tokens + excluded.input_tokens,
                output_tokens = flag_costs.output_tokens + excluded.output_tokens,
                cached_tokens = flag_costs.cached_tokens + excluded.cached_tokens,
                cost_cents = flag_costs.cost_cents + excluded.cost_cents,
                latency_ms = excluded.latency_ms,
                created_at = excluded.created_at
            """,
            (
                flag_id,
                model,
                input_tokens,
                output_tokens,
                cached_tokens,
                cost_cents,
                latency_ms,
                iso(now_utc()),
            ),
        )
        await db.commit()


async def get_flag_cost(
    db_path: Path, flag_id: int
) -> dict[str, Any] | None:
    if not db_path.exists():
        return None
    try:
        async with aiosqlite.connect(str(db_path)) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM flag_costs WHERE flag_id = ?", (flag_id,)
            ) as cur:
                row = await cur.fetchone()
    except aiosqlite.OperationalError:
        return None
    return dict(row) if row else None


async def sum_flag_costs_for_run_window(
    db_path: Path, *, since_iso: str, until_iso: str
) -> dict[str, int]:
    """Same as sum_flag_costs_since but bounded — useful for per-run cost."""
    out = {"total_cost_cents": 0, "total_calls": 0, "total_cached_tokens": 0}
    if not db_path.exists():
        return out
    try:
        async with aiosqlite.connect(str(db_path)) as db, db.execute(
            "SELECT COALESCE(SUM(cost_cents), 0), COUNT(*), "
            "COALESCE(SUM(cached_tokens), 0) "
            "FROM flag_costs WHERE created_at >= ? AND created_at <= ?",
            (since_iso, until_iso),
        ) as cur:
            row = await cur.fetchone()
    except aiosqlite.OperationalError:
        return out
    if row:
        out["total_cost_cents"] = int(row[0] or 0)
        out["total_calls"] = int(row[1] or 0)
        out["total_cached_tokens"] = int(row[2] or 0)
    return out


async def sum_flag_costs_since(
    db_path: Path, *, since_iso: str
) -> dict[str, int]:
    """Return {total_cost_cents, total_calls} for cost rollups."""
    out = {"total_cost_cents": 0, "total_calls": 0, "total_cached_tokens": 0}
    if not db_path.exists():
        return out
    try:
        async with aiosqlite.connect(str(db_path)) as db, db.execute(
            "SELECT COALESCE(SUM(cost_cents), 0), COUNT(*), "
            "COALESCE(SUM(cached_tokens), 0) "
            "FROM flag_costs WHERE created_at >= ?",
            (since_iso,),
        ) as cur:
            row = await cur.fetchone()
    except aiosqlite.OperationalError:
        return out
    if row:
        out["total_cost_cents"] = int(row[0] or 0)
        out["total_calls"] = int(row[1] or 0)
        out["total_cached_tokens"] = int(row[2] or 0)
    return out


async def get_flag(db_path: Path, flag_id: int) -> dict[str, Any] | None:
    if not db_path.exists():
        return None
    try:
        async with aiosqlite.connect(str(db_path)) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT f.*, m.category, m.question, m.slug,
                       m.daily_volume_usd_cents, m.resolves_at, m.resolved_outcome,
                       w.nonce, w.funding_source, w.funding_first_deposit_at,
                       w.lifetime_volume_cents, w.lifetime_trades, w.first_seen_at,
                       w.display_name, w.owner_address
                FROM flags f
                LEFT JOIN markets m ON m.id = f.market_id
                LEFT JOIN wallets w ON w.address = f.wallet_address
                WHERE f.id = ?
                """,
                (flag_id,),
            ) as cur:
                row = await cur.fetchone()
    except aiosqlite.OperationalError:
        return None
    return dict(row) if row else None


# ── row helpers ──────────────────────────────────────────


def _row_to_market(r: aiosqlite.Row) -> Market:
    return Market(
        id=r["id"],
        slug=r["slug"],
        question=r["question"],
        category=r["category"],
        created_at=parse_iso(r["created_at"]),
        resolves_at=parse_iso(r["resolves_at"]) if r["resolves_at"] else None,
        resolved_outcome=r["resolved_outcome"],
        resolved_at=parse_iso(r["resolved_at"]) if r["resolved_at"] else None,
        daily_volume_usd_cents=r["daily_volume_usd_cents"],
        metadata=json.loads(r["metadata_json"]) if r["metadata_json"] else {},
    )


def _row_to_wallet(r: aiosqlite.Row) -> Wallet:
    return Wallet(
        address=r["address"],
        first_seen_at=parse_iso(r["first_seen_at"]),
        nonce=r["nonce"],
        funding_source=r["funding_source"],
        funding_first_deposit_at=(
            parse_iso(r["funding_first_deposit_at"])
            if r["funding_first_deposit_at"]
            else None
        ),
        lifetime_volume_cents=int(r["lifetime_volume_cents"] or 0),
        lifetime_trades=int(r["lifetime_trades"] or 0),
        display_name=r["display_name"],
        owner_address=r["owner_address"],
        metadata=json.loads(r["metadata_json"]) if r["metadata_json"] else {},
        last_profiled_at=(
            parse_iso(r["last_profiled_at"]) if r["last_profiled_at"] else None
        ),
    )


def _row_to_trade(r: aiosqlite.Row) -> TradeEvent:
    return TradeEvent(
        id=r["id"],
        wallet_address=r["wallet_address"],
        market_id=r["market_id"],
        side=r["side"],
        outcome=r["outcome"],
        size_shares=int(r["size_shares"]),
        price_cents=int(r["price_cents"]),
        timestamp=parse_iso(r["timestamp"]),
        tx_hash=r["tx_hash"],
    )


# ── paper runs / positions / fills ───────────────────────


async def create_paper_run(
    db_path: Path,
    *,
    name: str,
    starting_balance_cents: int,
    config_snapshot: dict[str, Any],
    notes: str | None = None,
    profile_name: str = "systematic",
    profile_snapshot: dict[str, Any] | None = None,
    tag: str | None = None,
) -> int:
    """Create a run row. Returns new id.

    Addendum §4.2/§4.4: persists profile_name + profile_snapshot_json + tag.
    """
    async with aiosqlite.connect(str(db_path)) as db:
        cur = await db.execute(
            """
            INSERT INTO paper_runs(
                name, started_at, config_json,
                starting_balance_cents, current_balance_cents, notes,
                profile_name, profile_snapshot_json, tag
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                name,
                iso(now_utc()),
                json.dumps(config_snapshot, default=str),
                starting_balance_cents,
                starting_balance_cents,
                notes,
                profile_name,
                json.dumps(profile_snapshot or {}, default=str),
                tag,
            ),
        )
        new_id = int(cur.lastrowid or 0)
        await cur.close()
        await db.commit()
    return new_id


async def list_paper_runs_by_tag(
    db_path: Path, *, tag: str
) -> list[dict[str, Any]]:
    """Return all runs sharing a tag. Addendum §4.4."""
    if not db_path.exists():
        return []
    try:
        async with aiosqlite.connect(str(db_path)) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM paper_runs WHERE tag = ? ORDER BY started_at ASC",
                (tag,),
            ) as cur:
                rows = await cur.fetchall()
    except aiosqlite.OperationalError:
        return []
    return [dict(r) for r in rows]


async def find_open_run_by_name(
    db_path: Path, *, name: str, tag: str | None = None
) -> dict[str, Any] | None:
    """Return the oldest still-open run with this name (and tag), or None.

    Lets `live` reuse an existing run across restarts instead of spawning a
    fresh paper_run every boot.
    """
    if not db_path.exists():
        return None
    try:
        async with aiosqlite.connect(str(db_path)) as db:
            db.row_factory = aiosqlite.Row
            if tag is None:
                q = ("SELECT * FROM paper_runs WHERE name = ? AND tag IS NULL "
                     "AND ended_at IS NULL ORDER BY started_at ASC LIMIT 1")
                args: tuple[Any, ...] = (name,)
            else:
                q = ("SELECT * FROM paper_runs WHERE name = ? AND tag = ? "
                     "AND ended_at IS NULL ORDER BY started_at ASC LIMIT 1")
                args = (name, tag)
            async with db.execute(q, args) as cur:
                row = await cur.fetchone()
    except aiosqlite.OperationalError:
        return None
    return dict(row) if row else None


async def list_paper_runs_by_ids(
    db_path: Path, *, run_ids: list[int]
) -> list[dict[str, Any]]:
    if not run_ids or not db_path.exists():
        return []
    placeholders = ",".join("?" for _ in run_ids)
    try:
        async with aiosqlite.connect(str(db_path)) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                f"SELECT * FROM paper_runs WHERE id IN ({placeholders}) "
                "ORDER BY started_at ASC",
                tuple(run_ids),
            ) as cur:
                rows = await cur.fetchall()
    except aiosqlite.OperationalError:
        return []
    return [dict(r) for r in rows]


async def end_paper_run(
    db_path: Path, run_id: int, *, notes: str | None = None
) -> None:
    async with aiosqlite.connect(str(db_path)) as db:
        await db.execute(
            "UPDATE paper_runs SET ended_at = ?, notes = COALESCE(?, notes) "
            "WHERE id = ?",
            (iso(now_utc()), notes, run_id),
        )
        await db.commit()


async def pause_paper_run(
    db_path: Path, run_id: int, *, reason: str
) -> None:
    async with aiosqlite.connect(str(db_path)) as db:
        await db.execute(
            "UPDATE paper_runs SET paused_at = ?, pause_reason = ? WHERE id = ?",
            (iso(now_utc()), reason, run_id),
        )
        await db.commit()


async def resume_paper_run(db_path: Path, run_id: int) -> None:
    async with aiosqlite.connect(str(db_path)) as db:
        await db.execute(
            "UPDATE paper_runs SET paused_at = NULL, pause_reason = NULL "
            "WHERE id = ?",
            (run_id,),
        )
        await db.commit()


async def get_paper_run(
    db_path: Path, run_id: int
) -> dict[str, Any] | None:
    if not db_path.exists():
        return None
    try:
        async with aiosqlite.connect(str(db_path)) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM paper_runs WHERE id = ?", (run_id,)
            ) as cur:
                row = await cur.fetchone()
    except aiosqlite.OperationalError:
        return None
    return dict(row) if row else None


async def list_paper_runs(
    db_path: Path, *, limit: int = 50
) -> list[dict[str, Any]]:
    if not db_path.exists():
        return []
    try:
        async with aiosqlite.connect(str(db_path)) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM paper_runs ORDER BY started_at DESC LIMIT ?",
                (limit,),
            ) as cur:
                rows = await cur.fetchall()
    except aiosqlite.OperationalError:
        return []
    return [dict(r) for r in rows]


async def adjust_run_balance(
    db_path: Path, run_id: int, delta_cents: int
) -> int:
    """Apply a signed delta to the run's current_balance_cents. Returns new balance."""
    async with aiosqlite.connect(str(db_path)) as db:
        await db.execute(
            "UPDATE paper_runs SET current_balance_cents = current_balance_cents + ? "
            "WHERE id = ?",
            (delta_cents, run_id),
        )
        async with db.execute(
            "SELECT current_balance_cents FROM paper_runs WHERE id = ?", (run_id,)
        ) as cur:
            row = await cur.fetchone()
        await db.commit()
    return int(row[0]) if row and row[0] is not None else 0


async def write_paper_position(
    db_path: Path,
    *,
    run_id: int,
    market_id: str,
    outcome: str,
    size_shares: int,
    avg_entry_price_cents: int,
    source_flag_id: int | None,
    source_wallet: str | None,
    signal_composite: float | None = None,
    signal_multiplier: float | None = None,
) -> int:
    async with aiosqlite.connect(str(db_path)) as db:
        cur = await db.execute(
            """
            INSERT INTO paper_positions(
                run_id, market_id, outcome, size_shares, avg_entry_price_cents,
                opened_at, source_flag_id, source_wallet, status,
                signal_composite, signal_multiplier
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?)
            """,
            (
                run_id, market_id, outcome, size_shares, avg_entry_price_cents,
                iso(now_utc()),
                source_flag_id,
                source_wallet.lower() if source_wallet else None,
                signal_composite,
                signal_multiplier,
            ),
        )
        new_id = int(cur.lastrowid or 0)
        await cur.close()
        await db.commit()
    return new_id


async def update_position_size(
    db_path: Path,
    position_id: int,
    *,
    new_size_shares: int,
    new_avg_entry_price_cents: int,
) -> None:
    async with aiosqlite.connect(str(db_path)) as db:
        await db.execute(
            "UPDATE paper_positions SET size_shares = ?, avg_entry_price_cents = ? "
            "WHERE id = ?",
            (new_size_shares, new_avg_entry_price_cents, position_id),
        )
        await db.commit()


async def close_position(
    db_path: Path,
    position_id: int,
    *,
    realized_pnl_cents: int,
    status: str = "RESOLVED",
) -> None:
    async with aiosqlite.connect(str(db_path)) as db:
        await db.execute(
            "UPDATE paper_positions SET closed_at = ?, realized_pnl_cents = ?, "
            "status = ? WHERE id = ?",
            (iso(now_utc()), realized_pnl_cents, status, position_id),
        )
        await db.commit()


async def get_position(
    db_path: Path, position_id: int
) -> dict[str, Any] | None:
    if not db_path.exists():
        return None
    try:
        async with aiosqlite.connect(str(db_path)) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM paper_positions WHERE id = ?", (position_id,)
            ) as cur:
                row = await cur.fetchone()
    except aiosqlite.OperationalError:
        return None
    return dict(row) if row else None


async def get_open_position_for_market(
    db_path: Path, run_id: int, market_id: str
) -> dict[str, Any] | None:
    """Spec §7.6 — max 1 OPEN position per (run, market)."""
    if not db_path.exists():
        return None
    try:
        async with aiosqlite.connect(str(db_path)) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM paper_positions "
                "WHERE run_id = ? AND market_id = ? AND status = 'OPEN' "
                "LIMIT 1",
                (run_id, market_id),
            ) as cur:
                row = await cur.fetchone()
    except aiosqlite.OperationalError:
        return None
    return dict(row) if row else None


async def list_open_positions(
    db_path: Path, run_id: int
) -> list[dict[str, Any]]:
    if not db_path.exists():
        return []
    try:
        async with aiosqlite.connect(str(db_path)) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM paper_positions "
                "WHERE run_id = ? AND status = 'OPEN' "
                "ORDER BY opened_at",
                (run_id,),
            ) as cur:
                rows = await cur.fetchall()
    except aiosqlite.OperationalError:
        return []
    return [dict(r) for r in rows]


async def list_open_positions_for_market(
    db_path: Path, market_id: str
) -> list[dict[str, Any]]:
    """Used by the resolution handler — closes positions across all runs."""
    if not db_path.exists():
        return []
    try:
        async with aiosqlite.connect(str(db_path)) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM paper_positions "
                "WHERE market_id = ? AND status = 'OPEN'",
                (market_id,),
            ) as cur:
                rows = await cur.fetchall()
    except aiosqlite.OperationalError:
        return []
    return [dict(r) for r in rows]


async def write_paper_fill(
    db_path: Path,
    *,
    run_id: int,
    position_id: int,
    side: str,
    size_shares: int,
    fill_price_cents: int,
    intended_price_cents: int,
    slippage_cents: int,
    latency_ms: int,
    fee_cents: int,
) -> int:
    async with aiosqlite.connect(str(db_path)) as db:
        cur = await db.execute(
            """
            INSERT INTO paper_fills(
                run_id, position_id, side, size_shares, fill_price_cents,
                intended_price_cents, slippage_cents, latency_ms, fee_cents,
                timestamp
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id, position_id, side, size_shares, fill_price_cents,
                intended_price_cents, slippage_cents, latency_ms, fee_cents,
                iso(now_utc()),
            ),
        )
        new_id = int(cur.lastrowid or 0)
        await cur.close()
        await db.commit()
    return new_id


async def list_paper_fills(
    db_path: Path, run_id: int
) -> list[dict[str, Any]]:
    if not db_path.exists():
        return []
    try:
        async with aiosqlite.connect(str(db_path)) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM paper_fills WHERE run_id = ? ORDER BY timestamp",
                (run_id,),
            ) as cur:
                rows = await cur.fetchall()
    except aiosqlite.OperationalError:
        return []
    return [dict(r) for r in rows]


async def count_flags_since(
    db_path: Path, *, since_iso: str
) -> int:
    """Kill switch #2 input — flags/hour rolling count."""
    if not db_path.exists():
        return 0
    try:
        async with aiosqlite.connect(str(db_path)) as db, db.execute(
            "SELECT COUNT(*) FROM flags WHERE created_at >= ?",
            (since_iso,),
        ) as cur:
            row = await cur.fetchone()
    except aiosqlite.OperationalError:
        return 0
    return int(row[0]) if row and row[0] is not None else 0


# ── intel messages (Tier-3 Telegram scraper) ─────────────


async def write_intel_message(
    db_path: Path,
    *,
    source: str,
    external_id: str,
    posted_at: str,
    author: str | None,
    text: str,
    wallets: list[str],
    market_slugs: list[str],
    categories: list[str],
    raw: dict[str, Any] | None = None,
) -> int | None:
    """Insert an intel_messages row; ignore if (source, external_id) exists.

    Returns the new row id, or None on conflict.
    """
    async with aiosqlite.connect(str(db_path)) as db:
        cur = await db.execute(
            """
            INSERT OR IGNORE INTO intel_messages(
                source, external_id, posted_at, author, text,
                wallets_json, market_slugs_json, categories_json,
                raw_json, ingested_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source, external_id, posted_at, author, text,
                json.dumps(wallets), json.dumps(market_slugs),
                json.dumps(categories),
                json.dumps(raw, default=str) if raw else None,
                iso(now_utc()),
            ),
        )
        new_id = cur.lastrowid if (cur.rowcount or 0) > 0 else None
        await cur.close()
        await db.commit()
    return int(new_id) if new_id is not None else None


async def list_intel_messages(
    db_path: Path, *, source: str | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    if not db_path.exists():
        return []
    query = "SELECT * FROM intel_messages"
    args: list[Any] = []
    if source is not None:
        query += " WHERE source = ?"
        args.append(source)
    query += " ORDER BY posted_at DESC LIMIT ?"
    args.append(limit)
    try:
        async with aiosqlite.connect(str(db_path)) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(query, args) as cur:
                rows = await cur.fetchall()
    except aiosqlite.OperationalError:
        return []
    return [dict(r) for r in rows]


async def max_intel_external_id(
    db_path: Path, *, source: str
) -> int | None:
    """Highest numeric external_id seen for this source, or None."""
    if not db_path.exists():
        return None
    try:
        async with aiosqlite.connect(str(db_path)) as db, db.execute(
            "SELECT MAX(CAST(external_id AS INTEGER)) FROM intel_messages "
            "WHERE source = ?",
            (source,),
        ) as cur:
            row = await cur.fetchone()
    except aiosqlite.OperationalError:
        return None
    if row is None or row[0] is None:
        return None
    return int(row[0])


async def upsert_known_insider(
    db_path: Path,
    *,
    label: str,
    address: str | None,
    source: str = "operator",
    source_message_id: int | None = None,
    notes: str | None = None,
) -> None:
    """Insert or update a known_insiders row. Unique by label."""
    async with aiosqlite.connect(str(db_path)) as db:
        await db.execute(
            """
            INSERT INTO known_insiders(
                label, address, added_at, notes, source, source_message_id
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(label) DO UPDATE SET
                address = excluded.address,
                notes = COALESCE(excluded.notes, known_insiders.notes),
                source = excluded.source,
                source_message_id = excluded.source_message_id
            """,
            (
                label, address.lower() if address else None,
                iso(now_utc()), notes, source, source_message_id,
            ),
        )
        await db.commit()


async def list_known_insiders(
    db_path: Path, *, source: str | None = None
) -> list[dict[str, Any]]:
    if not db_path.exists():
        return []
    query = "SELECT * FROM known_insiders"
    args: list[Any] = []
    if source is not None:
        query += " WHERE source = ?"
        args.append(source)
    query += " ORDER BY added_at DESC"
    try:
        async with aiosqlite.connect(str(db_path)) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(query, args) as cur:
                rows = await cur.fetchall()
    except aiosqlite.OperationalError:
        return []
    return [dict(r) for r in rows]


# ── meta key/value (timing baselines etc.) ───────────────


async def meta_set_json(
    db_path: Path, *, key: str, value: dict[str, Any] | list[Any]
) -> None:
    async with aiosqlite.connect(str(db_path)) as db:
        await db.execute(
            """
            INSERT INTO meta(key, value_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value_json = excluded.value_json,
                updated_at = excluded.updated_at
            """,
            (key, json.dumps(value, default=str), iso(now_utc())),
        )
        await db.commit()


async def meta_get_json(
    db_path: Path, *, key: str
) -> dict[str, Any] | list[Any] | None:
    if not db_path.exists():
        return None
    try:
        async with aiosqlite.connect(str(db_path)) as db, db.execute(
            "SELECT value_json FROM meta WHERE key = ?", (key,)
        ) as cur:
            row = await cur.fetchone()
    except aiosqlite.OperationalError:
        return None
    if row is None or row[0] is None:
        return None
    try:
        loaded: Any = json.loads(str(row[0]))
    except json.JSONDecodeError:
        return None
    if isinstance(loaded, dict | list):
        return loaded
    return None


# ── orderbook snapshots (G8) ─────────────────────────────


async def write_orderbook_snapshot(
    db_path: Path,
    *,
    market_id: str,
    outcome: str,
    asks: list[dict[str, int]],
    bids: list[dict[str, int]],
) -> int:
    async with aiosqlite.connect(str(db_path)) as db:
        cur = await db.execute(
            """
            INSERT INTO orderbook_snapshots(
                market_id, outcome, timestamp, asks_json, bids_json
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                market_id, outcome, iso(now_utc()),
                json.dumps(asks), json.dumps(bids),
            ),
        )
        new_id = int(cur.lastrowid or 0)
        await cur.close()
        await db.commit()
    return new_id


async def get_nearest_orderbook_snapshot(
    db_path: Path,
    *,
    market_id: str,
    outcome: str,
    at_iso: str,
    max_age_seconds: int = 300,
) -> dict[str, Any] | None:
    """Return the newest snapshot at or before `at_iso`, within max_age seconds."""
    if not db_path.exists():
        return None
    try:
        async with aiosqlite.connect(str(db_path)) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT * FROM orderbook_snapshots
                WHERE market_id = ? AND outcome = ? AND timestamp <= ?
                  AND (julianday(?) - julianday(timestamp)) * 86400 <= ?
                ORDER BY timestamp DESC LIMIT 1
                """,
                (market_id, outcome, at_iso, at_iso, max_age_seconds),
            ) as cur:
                row = await cur.fetchone()
    except aiosqlite.OperationalError:
        return None
    return dict(row) if row else None


# ── row helpers ──────────────────────────────────────────


def _row_to_flag(r: aiosqlite.Row) -> Flag:
    return Flag(
        id=int(r["id"]),
        wallet_address=r["wallet_address"],
        market_id=r["market_id"],
        trade_id=r["trade_id"],
        detector_name=r["detector_name"],
        raw_score=float(r["raw_score"]),
        composite_score=float(r["composite_score"]) if r["composite_score"] is not None else None,
        components=json.loads(r["components_json"]) if r["components_json"] else {},
        investigator_verdict=r["investigator_verdict"],
        investigator_reasoning=r["investigator_reasoning"],
        created_at=parse_iso(r["created_at"]),
        acted_on=bool(r["acted_on"]),
    )
