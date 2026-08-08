"""Direct P&L calculation: what did cohort wallets actually make on
resolved markets? No simulator, no fill model, no position caps —
just look at what each cohort trade was worth at resolution.

For each cohort BUY trade on a market that resolved:
    entry_cost = size * price_cents
    proceeds   = size * (100 if outcome matches resolved_outcome else 0)
    pnl        = proceeds - entry_cost
    roi_pct    = pnl / entry_cost

Reports total P&L, win rate, mean ROI, and per-wallet/per-niche breakdown.

This is the "if we'd mirrored these wallets exactly" upper-bound: it
ignores fees, slippage, and the bankroll/position caps. So the real
strategy P&L would be lower, but if THIS number is negative the
strategy is dead in the water regardless.

Run from repo root:
    .venv/Scripts/python.exe scripts/cohort_pnl_on_resolved.py
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import aiosqlite


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=Path("polysim.db"))
    parser.add_argument(
        "--general-only", action="store_true",
        help="Restrict to cohort wallets in the 'general' pool "
        "(drops niche pools entirely).",
    )
    parser.add_argument(
        "--categories", default="",
        help="Comma-separated market categories to include (empty = all). "
        "Try 'politics,sports'.",
    )
    parser.add_argument(
        "--top-n-wallets", type=int, default=0,
        help="Restrict to top-N cohort wallets by lifetime notional. "
        "0 = all cohort wallets.",
    )
    parser.add_argument(
        "--since", default="",
        help="Only count cohort BUY trades with timestamp >= this ISO date.",
    )
    parser.add_argument(
        "--until", default="",
        help="Only count cohort BUY trades with timestamp < this ISO date.",
    )
    parser.add_argument(
        "--bankroll-cents", type=int, default=0,
        help="If >0, simulate a fixed bankroll: every trade sized at "
        "`pct_per_position` of CURRENT balance, no overdraft. P&L "
        "applied chronologically.",
    )
    parser.add_argument(
        "--pct-per-position", type=float, default=0.02,
        help="Fraction of current bankroll allocated per trade (default 2%%).",
    )
    args = parser.parse_args()

    cats = [c.strip().lower() for c in args.categories.split(",") if c.strip()]
    pool_clause = (
        " AND wd.cohort_niche = 'general'" if args.general_only else ""
    )

    async with aiosqlite.connect(str(args.db)) as conn:
        conn.row_factory = aiosqlite.Row
        # Restrict to top-N wallets by lifetime notional if requested.
        wallet_filter_clause = ""
        if args.top_n_wallets > 0:
            async with conn.execute(
                """
                SELECT t.wallet_address
                FROM trades t
                JOIN wallets_discovery wd ON wd.address = t.wallet_address
                WHERE wd.is_cohort = 1
                """ + pool_clause + """
                GROUP BY t.wallet_address
                ORDER BY SUM(t.size_shares * t.price_cents) DESC
                LIMIT ?
                """,
                (args.top_n_wallets,),
            ) as cur:
                top = [str(r[0]) for r in await cur.fetchall()]
            if top:
                placeholders = ",".join("?" * len(top))
                wallet_filter_clause = (
                    f" AND t.wallet_address IN ({placeholders})"
                )

        # All cohort BUY trades on resolved markets.
        date_clause = ""
        date_params: list[object] = []
        if args.since:
            date_clause += " AND t.timestamp >= ?"
            date_params.append(args.since)
        if args.until:
            date_clause += " AND t.timestamp < ?"
            date_params.append(args.until)
        sql = """
            SELECT t.wallet_address, t.market_id, t.outcome, t.side,
                   t.size_shares, t.price_cents, t.timestamp,
                   m.resolved_outcome, m.slug, m.category,
                   wd.cohort_niche
            FROM trades t
            JOIN markets m ON m.id = t.market_id
            JOIN wallets_discovery wd ON wd.address = t.wallet_address
            WHERE wd.is_cohort = 1
              AND m.resolved_outcome IS NOT NULL
              AND m.resolved_outcome IN ('YES', 'NO')
              AND t.side = 'BUY'
        """ + pool_clause + wallet_filter_clause + date_clause + (
            " ORDER BY t.timestamp ASC"
        )
        params: list[object] = []
        if wallet_filter_clause:
            params.extend(top)
        params.extend(date_params)
        async with conn.execute(sql, params) as cur:
            rows = list(await cur.fetchall())

    if cats:
        rows = [
            r for r in rows
            if (str(r["category"] or "").lower() in cats)
        ]

    if not rows:
        print("no cohort BUY trades on resolved YES/NO markets.")
        return 0

    print(f"sample: {len(rows)} cohort BUY trades on resolved binary markets")
    print()

    # Tallies.
    total_cost_cents = 0
    total_proceeds_cents = 0
    wins = 0
    losses = 0
    by_wallet: dict[str, dict[str, int]] = defaultdict(
        lambda: {"n": 0, "wins": 0, "cost": 0, "proceeds": 0}
    )
    by_niche: dict[str, dict[str, int]] = defaultdict(
        lambda: {"n": 0, "wins": 0, "cost": 0, "proceeds": 0}
    )
    by_market: dict[str, dict[str, int]] = defaultdict(
        lambda: {"n": 0, "wins": 0, "cost": 0, "proceeds": 0,
                 "slug": "", "resolved": ""}
    )

    for r in rows:
        size = int(r["size_shares"])
        entry = int(r["price_cents"])
        outcome = str(r["outcome"]).upper()
        resolved = str(r["resolved_outcome"]).upper()
        won = (outcome == resolved)
        cost = size * entry
        proceeds = size * (100 if won else 0)
        pnl = proceeds - cost
        total_cost_cents += cost
        total_proceeds_cents += proceeds
        if won:
            wins += 1
        else:
            losses += 1
        wallet = str(r["wallet_address"])
        bw = by_wallet[wallet]
        bw["n"] += 1
        bw["wins"] += 1 if won else 0
        bw["cost"] += cost
        bw["proceeds"] += proceeds
        niche = str(r["cohort_niche"] or "general")
        bn = by_niche[niche]
        bn["n"] += 1
        bn["wins"] += 1 if won else 0
        bn["cost"] += cost
        bn["proceeds"] += proceeds
        mid = str(r["market_id"])
        bm = by_market[mid]
        bm["n"] += 1
        bm["wins"] += 1 if won else 0
        bm["cost"] += cost
        bm["proceeds"] += proceeds
        bm["slug"] = str(r["slug"] or "")
        bm["resolved"] = resolved
        _ = pnl  # could log but skip for now

    pnl_total = total_proceeds_cents - total_cost_cents
    n = wins + losses
    print("=== Headline ===")
    print(f"trades: {n}  wins: {wins}  losses: {losses}  "
          f"win_rate: {wins/n*100:.1f}%")
    print(f"total cost (gross capital deployed): "
          f"${total_cost_cents/100:,.2f}")
    print(f"total proceeds at resolution:        "
          f"${total_proceeds_cents/100:,.2f}")
    print(f"realized P&L:                        "
          f"${pnl_total/100:+,.2f}")
    if total_cost_cents > 0:
        roi = pnl_total / total_cost_cents
        print(f"gross ROI:                           {roi*100:+.2f}%")
    print()

    print("=== By niche ===")
    print(f"  {'niche':<14} {'n':>5} {'win%':>6} {'cost':>14} "
          f"{'proceeds':>14} {'pnl':>14} {'roi':>7}")
    for niche, d in sorted(by_niche.items(), key=lambda kv: -kv[1]["n"]):
        pnl = d["proceeds"] - d["cost"]
        roi = (pnl / d["cost"]) if d["cost"] else 0.0
        print(
            f"  {niche:<14} {d['n']:>5} "
            f"{d['wins']/d['n']*100:>5.1f}% "
            f"${d['cost']/100:>13,.0f} "
            f"${d['proceeds']/100:>13,.0f} "
            f"${pnl/100:>+13,.0f} "
            f"{roi*100:>6.1f}%"
        )
    print()

    print("=== Top 15 cohort wallets by P&L ===")
    wallet_rows = sorted(
        by_wallet.items(),
        key=lambda kv: -(kv[1]["proceeds"] - kv[1]["cost"]),
    )
    print(f"  {'wallet':<16} {'n':>4} {'win%':>5} {'cost':>11} "
          f"{'pnl':>11} {'roi':>7}")
    for w, d in wallet_rows[:15]:
        pnl = d["proceeds"] - d["cost"]
        roi = (pnl / d["cost"]) if d["cost"] else 0.0
        print(
            f"  {w[:14]+'..':<16} {d['n']:>4} "
            f"{d['wins']/d['n']*100:>4.1f}% "
            f"${d['cost']/100:>10,.0f} "
            f"${pnl/100:>+10,.0f} "
            f"{roi*100:>6.1f}%"
        )
    print()

    print("=== Bottom 5 cohort wallets by P&L ===")
    for w, d in wallet_rows[-5:]:
        pnl = d["proceeds"] - d["cost"]
        roi = (pnl / d["cost"]) if d["cost"] else 0.0
        print(
            f"  {w[:14]+'..':<16} {d['n']:>4} "
            f"{d['wins']/d['n']*100:>4.1f}% "
            f"${d['cost']/100:>10,.0f} "
            f"${pnl/100:>+10,.0f} "
            f"{roi*100:>6.1f}%"
        )
    print()

    print("=== Top 5 markets by cohort P&L contribution ===")
    market_rows = sorted(
        by_market.items(),
        key=lambda kv: -(kv[1]["proceeds"] - kv[1]["cost"]),
    )
    for _mid, d in market_rows[:5]:
        pnl = d["proceeds"] - d["cost"]
        print(f"  {d['slug'][:55]:<55}  "
              f"resolved={d['resolved']:<3}  n={d['n']:<4}  "
              f"pnl=${pnl/100:>+,.0f}")
    print()
    print("=== Bottom 5 markets by cohort P&L contribution ===")
    for _mid, d in market_rows[-5:]:
        pnl = d["proceeds"] - d["cost"]
        print(f"  {d['slug'][:55]:<55}  "
              f"resolved={d['resolved']:<3}  n={d['n']:<4}  "
              f"pnl=${pnl/100:>+,.0f}")

    # ── Realistic $X bankroll simulation ──
    if args.bankroll_cents > 0:
        print()
        print(f"=== Bankroll simulation: "
              f"${args.bankroll_cents/100:,.2f} starting, "
              f"{args.pct_per_position*100:.1f}% per trade ===")
        balance = args.bankroll_cents
        starting = balance
        sim_wins = 0
        sim_losses = 0
        sim_skipped = 0
        sim_total_cost = 0
        sim_total_proceeds = 0
        per_trade_log: list[tuple[str, int, int, int]] = []
        for r in rows:  # already chronological
            entry = int(r["price_cents"])
            outcome = str(r["outcome"]).upper()
            resolved = str(r["resolved_outcome"]).upper()
            won = (outcome == resolved)
            target_cost = int(balance * args.pct_per_position)
            if entry <= 0 or target_cost < entry:
                sim_skipped += 1
                continue
            sim_size = target_cost // entry
            cost = sim_size * entry
            proceeds = sim_size * (100 if won else 0)
            pnl = proceeds - cost
            balance += pnl
            sim_total_cost += cost
            sim_total_proceeds += proceeds
            if won:
                sim_wins += 1
            else:
                sim_losses += 1
            per_trade_log.append(
                (str(r["slug"] or "")[:40], cost, pnl, balance),
            )
        n_sim = sim_wins + sim_losses
        print(f"trades taken:     {n_sim}  (skipped {sim_skipped} for "
              f"min-size constraints)")
        print(f"wins / losses:    {sim_wins} / {sim_losses}  "
              f"({sim_wins/max(n_sim,1)*100:.1f}% win rate)")
        print(f"capital deployed: ${sim_total_cost/100:,.2f}")
        print(f"proceeds:         ${sim_total_proceeds/100:,.2f}")
        print(f"ending balance:   ${balance/100:,.2f}")
        delta = balance - starting
        pct = (delta / starting) * 100
        print(f"period return:    ${delta/100:+,.2f}  ({pct:+.2f}%)")
        if per_trade_log:
            print()
            print("First 10 trades:")
            for slug, c, p, bal in per_trade_log[:10]:
                print(f"  cost=${c/100:>6,.2f}  pnl=${p/100:>+7,.2f}  "
                      f"bal=${bal/100:>9,.2f}  {slug}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
