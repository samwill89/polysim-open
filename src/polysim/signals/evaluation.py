"""Measured-edge hooks for the signal layer.

Two honest questions, answered from stored rows only (works identically
on live data and on fixture-driven backtests):

  1. `signal_bucket_outcomes` — on *resolved* markets that had a signal:
     does higher conversation conviction line up with better realized
     P&L per position? (Composite terciles → per-bucket stats.)
  2. `signal_sizing_summary` — positions the executor actually adjusted
     (signal_multiplier ≠ NULL) vs untouched positions: realized P&L per
     position, win rate. This is the A/B the tournament variants create.

Realized-only by design: open-position MTM never enters these numbers
(MTM, realized, and exposure stay separate surfaces).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import aiosqlite


def _bucket_of(composite: float, edges: tuple[float, float]) -> str:
    lo, hi = edges
    if composite < lo:
        return "low"
    if composite < hi:
        return "mid"
    return "high"


async def signal_bucket_outcomes(
    db_path: Path,
    *,
    bucket_edges: tuple[float, float] = (0.45, 0.60),
) -> list[dict[str, Any]]:
    """Per composite-bucket realized outcomes on resolved markets.

    Uses each market's *latest signal before resolution* and every closed/
    resolved paper position on that market. Returns one row per bucket:
    {bucket, n_markets, n_positions, realized_pnl_cents, avg_pnl_cents,
     win_rate}.
    """
    if not db_path.exists():
        return []
    sql = """
        WITH last_sig AS (
            SELECT s.market_id, s.composite,
                   ROW_NUMBER() OVER (
                       PARTITION BY s.market_id ORDER BY s.ts DESC
                   ) AS rn
            FROM market_signals s
            JOIN markets m ON m.id = s.market_id
            WHERE m.resolved_outcome IS NOT NULL
              AND (m.resolved_at IS NULL OR s.ts <= m.resolved_at)
        )
        SELECT ls.market_id, ls.composite,
               p.id AS position_id, p.realized_pnl_cents
        FROM last_sig ls
        LEFT JOIN paper_positions p
               ON p.market_id = ls.market_id
              AND p.realized_pnl_cents IS NOT NULL
        WHERE ls.rn = 1
    """
    try:
        async with aiosqlite.connect(str(db_path)) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql) as cur:
                rows = await cur.fetchall()
    except aiosqlite.OperationalError:
        return []

    stats: dict[str, dict[str, Any]] = {}
    seen_markets: dict[str, set[str]] = {}
    for r in rows:
        bucket = _bucket_of(float(r["composite"]), bucket_edges)
        s = stats.setdefault(bucket, {
            "bucket": bucket, "n_markets": 0, "n_positions": 0,
            "realized_pnl_cents": 0, "wins": 0,
        })
        seen = seen_markets.setdefault(bucket, set())
        mid = str(r["market_id"])
        if mid not in seen:
            seen.add(mid)
            s["n_markets"] += 1
        if r["position_id"] is not None:
            pnl = int(r["realized_pnl_cents"])
            s["n_positions"] += 1
            s["realized_pnl_cents"] += pnl
            if pnl > 0:
                s["wins"] += 1

    out: list[dict[str, Any]] = []
    for bucket in ("low", "mid", "high"):
        bucket_stats = stats.get(bucket)
        if bucket_stats is None:
            continue
        n = int(bucket_stats["n_positions"])
        out.append({
            **bucket_stats,
            "avg_pnl_cents": (
                bucket_stats["realized_pnl_cents"] / n if n else 0.0
            ),
            "win_rate": (bucket_stats["wins"] / n if n else 0.0),
        })
    return out


async def signal_sizing_summary(db_path: Path) -> dict[str, Any]:
    """Signal-adjusted vs untouched positions, realized-only."""
    empty = {
        "adjusted": {"n": 0, "realized_pnl_cents": 0, "avg_pnl_cents": 0.0,
                     "win_rate": 0.0, "avg_multiplier": None},
        "untouched": {"n": 0, "realized_pnl_cents": 0, "avg_pnl_cents": 0.0,
                      "win_rate": 0.0},
    }
    if not db_path.exists():
        return empty
    try:
        async with aiosqlite.connect(str(db_path)) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT signal_multiplier, realized_pnl_cents "
                "FROM paper_positions WHERE realized_pnl_cents IS NOT NULL",
            ) as cur:
                rows = await cur.fetchall()
    except aiosqlite.OperationalError:
        return empty

    adj_pnl: list[int] = []
    adj_mults: list[float] = []
    unt_pnl: list[int] = []
    for r in rows:
        pnl = int(r["realized_pnl_cents"])
        if r["signal_multiplier"] is not None:
            adj_pnl.append(pnl)
            adj_mults.append(float(r["signal_multiplier"]))
        else:
            unt_pnl.append(pnl)

    def _summ(pnls: list[int]) -> dict[str, Any]:
        n = len(pnls)
        total = sum(pnls)
        wins = sum(1 for p in pnls if p > 0)
        return {
            "n": n,
            "realized_pnl_cents": total,
            "avg_pnl_cents": (total / n if n else 0.0),
            "win_rate": (wins / n if n else 0.0),
        }

    adjusted = _summ(adj_pnl)
    adjusted["avg_multiplier"] = (
        sum(adj_mults) / len(adj_mults) if adj_mults else None
    )
    return {"adjusted": adjusted, "untouched": _summ(unt_pnl)}


async def signal_coverage(db_path: Path) -> dict[str, Any]:
    """How much of the currently-open book has a fresh signal — operator
    visibility into whether the layer is actually running."""
    out = {"open_markets": 0, "with_any_signal": 0}
    if not db_path.exists():
        return out
    try:
        async with aiosqlite.connect(str(db_path)) as db:
            async with db.execute(
                "SELECT COUNT(DISTINCT market_id) FROM paper_positions "
                "WHERE status = 'OPEN'",
            ) as cur:
                row = await cur.fetchone()
                out["open_markets"] = int(row[0]) if row else 0
            async with db.execute(
                "SELECT COUNT(DISTINCT p.market_id) FROM paper_positions p "
                "JOIN market_signals s ON s.market_id = p.market_id "
                "WHERE p.status = 'OPEN'",
            ) as cur:
                row = await cur.fetchone()
                out["with_any_signal"] = int(row[0]) if row else 0
    except aiosqlite.OperationalError:
        return out
    return out


__all__ = ["signal_bucket_outcomes", "signal_coverage", "signal_sizing_summary"]
