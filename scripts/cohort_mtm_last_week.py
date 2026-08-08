"""Mark-to-market estimate of last week's cohort BUYs.

For positions that haven't yet resolved, MTM = size * (latest_market_price - entry_price).
'latest_market_price' is the price of the most recent trade we've seen on
the same (market_id, outcome). This isn't an orderbook mid — it's a
last-trade tape — but it's the cheapest reliable proxy.

Same simulator semantics as cohort_pnl_on_resolved.py with --bankroll-cents:
deploy `pct_per_position` of running balance per trade, skip if too small.

Run from repo root:
    .venv/Scripts/python.exe scripts/cohort_mtm_last_week.py \\
        --since 2026-04-21 --bankroll-cents 1000000 --pct-per-position 0.02
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import aiosqlite


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=Path("polysim.db"))
    parser.add_argument("--since", default="2026-04-21")
    parser.add_argument("--until", default="")
    parser.add_argument(
        "--general-only", action="store_true", default=True,
        help="Restrict to general-pool wallets (default after 2026-04-28 calibration)",
    )
    parser.add_argument(
        "--categories", default="politics,sports",
        help="Comma-separated market categories to include (default politics,sports)",
    )
    parser.add_argument("--bankroll-cents", type=int, default=1_000_000)
    parser.add_argument("--pct-per-position", type=float, default=0.02)
    args = parser.parse_args()

    cats = [c.strip().lower() for c in args.categories.split(",") if c.strip()]
    pool_clause = (
        " AND wd.cohort_niche = 'general'" if args.general_only else ""
    )
    cat_clause = ""
    cat_params: list[object] = []
    if cats:
        cat_clause = " AND LOWER(COALESCE(m.category,'')) IN (" + ",".join(
            "?" * len(cats)) + ")"
        cat_params.extend(cats)

    date_clause = " AND t.timestamp >= ?"
    date_params: list[object] = [args.since]
    if args.until:
        date_clause += " AND t.timestamp < ?"
        date_params.append(args.until)

    sql = """
        SELECT t.id, t.wallet_address, t.market_id, t.outcome, t.side,
               t.size_shares, t.price_cents, t.timestamp,
               m.resolved_outcome, m.slug, m.category
        FROM trades t
        JOIN markets m ON m.id = t.market_id
        JOIN wallets_discovery wd ON wd.address = t.wallet_address
        WHERE wd.is_cohort = 1
          AND t.side = 'BUY'
    """ + pool_clause + cat_clause + date_clause + " ORDER BY t.timestamp ASC"
    params: list[object] = [*cat_params, *date_params]

    async with aiosqlite.connect(str(args.db)) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(sql, params) as cur:
            cohort_buys = list(await cur.fetchall())

        # For each (market_id, outcome) appearing in the cohort buys, get
        # the most-recent trade price as the MTM mark.
        keys = {(str(r["market_id"]), str(r["outcome"]).upper())
                for r in cohort_buys}
        marks: dict[tuple[str, str], int] = {}
        for mid, outcome in keys:
            async with conn.execute(
                "SELECT price_cents FROM trades "
                "WHERE market_id = ? AND outcome = ? "
                "ORDER BY timestamp DESC LIMIT 1",
                (mid, outcome),
            ) as cur:
                row = await cur.fetchone()
            if row:
                marks[(mid, outcome)] = int(row[0])

    print("=== MTM estimate for last week's cohort BUYs ===")
    print(f"window:           {args.since} to "
          f"{args.until or '(now)'}")
    print(f"filters:          general-pool, categories={cats or 'all'}")
    print(f"raw cohort BUYs:  {len(cohort_buys):,}")
    print(f"unique (market,outcome) pairs: {len(keys)}")
    print()

    balance = args.bankroll_cents
    starting = balance
    deployed = 0
    skipped = 0
    n_taken = 0
    n_resolved_so_far = 0
    realized = 0
    unrealized = 0
    total_open_value = 0
    open_count = 0

    for r in cohort_buys:
        entry = int(r["price_cents"])
        outcome = str(r["outcome"]).upper()
        mid = str(r["market_id"])
        target_cost = int(balance * args.pct_per_position)
        if entry <= 0 or target_cost < entry:
            skipped += 1
            continue
        size = target_cost // entry
        if size <= 0:
            skipped += 1
            continue
        cost = size * entry
        # Reserve from balance.
        balance -= cost
        deployed += cost
        n_taken += 1

        resolved = r["resolved_outcome"]
        if resolved in ("YES", "NO"):
            won = (outcome == str(resolved).upper())
            proceeds = size * (100 if won else 0)
            balance += proceeds   # release back to bankroll
            realized += (proceeds - cost)
            n_resolved_so_far += 1
        else:
            mark_price = marks.get((mid, outcome), entry)
            position_value_now = size * mark_price
            unrealized += (position_value_now - cost)
            total_open_value += position_value_now
            open_count += 1

    ending_value = balance + total_open_value
    total_pnl = ending_value - starting

    print("=== Results ===")
    print(f"trades taken:                 {n_taken:,}")
    print(f"trades skipped (size < entry): {skipped:,}")
    print(f"  positions resolved:         {n_resolved_so_far}")
    print(f"  positions still open:       {open_count}")
    print()
    print(f"capital deployed:             ${deployed/100:>12,.2f}")
    print(f"realized P&L (resolved):      ${realized/100:>+12,.2f}")
    print(f"unrealized P&L (MTM):         ${unrealized/100:>+12,.2f}")
    print(f"total P&L (realized + MTM):   ${total_pnl/100:>+12,.2f}")
    print()
    print(f"starting balance:             ${starting/100:>12,.2f}")
    print(f"cash balance now:             ${balance/100:>12,.2f}")
    print(f"open position MTM value:      ${total_open_value/100:>12,.2f}")
    print(f"total portfolio value:        ${ending_value/100:>12,.2f}")
    period_return = (total_pnl / starting) * 100 if starting else 0.0
    print(f"period return:                {period_return:>+11.2f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
