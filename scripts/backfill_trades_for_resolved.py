"""Fetch trades for every recently-resolved market in the DB.

Closes the gap between `polysim ingest backfill-closed` (markets only, no
trades) and `polysim ingest backfill --days 90` (trades on ACTIVE markets).
Recently-resolved markets are by definition not active anymore, so they
get skipped by both. This script targets exactly that set.

Output: trade rows on markets we have ground truth for, enabling honest
P&L on backtest replays.

Run from repo root:
    .venv/Scripts/python.exe scripts/backfill_trades_for_resolved.py \\
        --since 2026-01-28 --max-trades-per-market 5000
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import aiosqlite

from polysim.db import dao
from polysim.ingest.polymarket_rest import PolymarketREST


async def _resolved_market_ids(
    db: Path, *, since: str, limit: int,
) -> list[str]:
    async with aiosqlite.connect(str(db)) as conn, conn.execute(
        "SELECT id FROM markets "
        "WHERE resolved_outcome IS NOT NULL "
        "AND resolves_at IS NOT NULL "
        "AND resolves_at >= ? "
        "ORDER BY resolves_at DESC LIMIT ?",
        (since, limit),
    ) as cur:
        rows = await cur.fetchall()
    return [str(r[0]) for r in rows]


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=Path("polysim.db"))
    parser.add_argument("--since", default="2026-01-28")
    parser.add_argument("--max-markets", type=int, default=200)
    parser.add_argument("--max-trades-per-market", type=int, default=5000)
    parser.add_argument("--page-size", type=int, default=500)
    args = parser.parse_args()

    market_ids = await _resolved_market_ids(
        args.db, since=args.since, limit=args.max_markets,
    )
    print(f"targeting {len(market_ids)} resolved markets since {args.since}")
    if not market_ids:
        print("nothing to fetch.")
        return 0

    cli = PolymarketREST()
    total_trades = 0
    markets_with_trades = 0
    try:
        for i, mid in enumerate(market_ids, start=1):
            offset = 0
            seen_for_this = 0
            while seen_for_this < args.max_trades_per_market:
                batch = await cli.get_trades(
                    market_id=mid, limit=args.page_size, offset=offset,
                )
                if not batch:
                    break
                await dao.insert_trades_batch(args.db, batch)
                seen_for_this += len(batch)
                offset += args.page_size
                if len(batch) < args.page_size:
                    break
            if seen_for_this > 0:
                markets_with_trades += 1
                total_trades += seen_for_this
            if i % 25 == 0:
                print(
                    f"  {i}/{len(market_ids)} markets processed, "
                    f"{total_trades} trades fetched, "
                    f"{markets_with_trades} markets had >0 trades"
                )
    finally:
        await cli.aclose()

    print()
    print(f"OK trades_fetched={total_trades}  "
          f"markets_with_trades={markets_with_trades}/{len(market_ids)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
