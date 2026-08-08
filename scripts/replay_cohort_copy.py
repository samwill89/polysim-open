"""Backfill replay using the new cohort-copy strategy.

Walks historical trades in [from, to], synthesizes a CohortCopy flag for
every cohort-wallet trade above the notional floor, dispatches each into
a fresh paper run (tagged 'cohort_copy_backfill'), and reports:

  * cohort_overlap_trades — how many trades in window were by cohort wallets
  * cohort_overlap_above_floor — subset above $X notional
  * flags_written / positions_opened / fills_written
  * P&L breakdown:
      - resolved_markets: positions on markets we have ground truth for
      - unresolved_open: still open at end of window
      - realized_pnl_cents on the resolved subset

Caveat: uses the *current* cohort (frozen on April 25). For point-in-time
backtest accuracy we'd need historical cohort snapshots — flagged below.

Run from repo root:
    .venv/Scripts/python.exe scripts/replay_cohort_copy.py \\
        --from 2026-04-20 --to 2026-04-28 --min-notional-cents 1000 \\
        --profile systematic
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import aiosqlite

from polysim.config import load_config
from polysim.live import _write_cohort_copy_flag
from polysim.paper.fill_model import FillModel
from polysim.paper.profile_executor import Dispatcher, ProfilePaperExecutor
from polysim.paper.run_manager import start_run
from polysim.profiles import load_profile


def _parse_date(s: str) -> datetime:
    if "T" in s:
        return datetime.fromisoformat(s).astimezone(UTC)
    return datetime.fromisoformat(s).replace(tzinfo=UTC)


async def _current_cohort(db: Path) -> set[str]:
    async with aiosqlite.connect(str(db)) as conn, conn.execute(
        "SELECT address FROM wallets_discovery WHERE is_cohort = 1"
    ) as cur:
        rows = await cur.fetchall()
    return {str(r[0]).lower() for r in rows if r and r[0]}


async def _trades_in_window(
    db: Path, *, start: datetime, end: datetime,
) -> list[dict[str, object]]:
    async with aiosqlite.connect(str(db)) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT id, wallet_address, market_id, side, outcome, "
            "size_shares, price_cents, timestamp "
            "FROM trades WHERE timestamp >= ? AND timestamp <= ? "
            "ORDER BY timestamp ASC",
            (start.isoformat(), end.isoformat()),
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--from", dest="from_date", required=True)
    parser.add_argument("--to", dest="to_date", required=True)
    parser.add_argument("--db", type=Path, default=Path("polysim.db"))
    parser.add_argument("--min-notional-cents", type=int, default=1_000)
    parser.add_argument("--profile", default="systematic")
    parser.add_argument("--max-open-positions", type=int, default=500)
    parser.add_argument("--config", type=Path, default=Path("config.yml"))
    args = parser.parse_args()

    start = _parse_date(args.from_date)
    end = _parse_date(args.to_date)
    cfg = load_config(args.config)

    print(f"backfill window: {start.isoformat()} -> {end.isoformat()}")
    print(f"min notional: ${args.min_notional_cents/100:.2f}  profile={args.profile}")
    print()

    cohort = await _current_cohort(args.db)
    print(f"current cohort: {len(cohort)} wallets")
    if not cohort:
        print("ERROR: empty cohort. Run `polysim discovery run` first.")
        return 2

    trades = await _trades_in_window(args.db, start=start, end=end)
    print(f"trades in window: {len(trades)}")

    # Stats pass.
    cohort_trades = [t for t in trades if str(t["wallet_address"]).lower() in cohort]
    above_floor = [
        t for t in cohort_trades
        if int(t["size_shares"]) * int(t["price_cents"]) >= args.min_notional_cents
    ]
    print(f"  cohort-wallet trades: {len(cohort_trades)}")
    print(f"  above floor:          {len(above_floor)}")
    if not above_floor:
        print("nothing to copy. exiting.")
        return 0

    # Open a fresh paper run for the replay.
    base_profile = load_profile(args.profile)
    # Lift max_open_positions for the offline run — at live cadence the
    # original 20-cap is fine, but in a compressed-time backfill we'd
    # otherwise plateau in seconds and reject ~99% of signal.
    profile = base_profile.model_copy(
        update={"max_open_positions": args.max_open_positions}
    )
    run_id = await start_run(
        args.db, cfg,
        profile=profile,
        name_override=f"cohort_copy_backfill-{profile.name}-{start.date()}",
        balance_override_cents=cfg.run.starting_balance_cents,
        tag=f"cohort_copy_backfill-{start.date()}-{end.date()}",
    )
    print(f"opened backfill run #{run_id}")
    print()

    fill_model = FillModel(
        detection_latency_p50_ms=cfg.fill_model.detection_latency_p50_ms,
        detection_latency_p95_ms=cfg.fill_model.detection_latency_p95_ms,
        decision_latency_p50_ms=cfg.fill_model.decision_latency_p50_ms,
        decision_latency_p95_ms=cfg.fill_model.decision_latency_p95_ms,
        slippage_ticks=cfg.fill_model.slippage_ticks,
        on_partial=cfg.fill_model.on_partial,
        fee_bps=cfg.fill_model.fee_bps,
        historical_pessimism_multiplier=cfg.fill_model.historical_pessimism_multiplier,
    )
    # Kill-switch is wall-clock-based — meaningless when backfill compresses
    # months of trades into seconds. Bump to effectively unlimited for the
    # offline run so we get realistic position counts.
    # Also lift bankroll.max_open_positions because the per-run cap (default
    # 20) plateaus the strategy on day 1 of any backtest.
    backfill_bankroll = cfg.bankroll.model_copy(
        update={
            "kill_flags_per_hour": 10_000_000,
            "max_open_positions": args.max_open_positions,
        }
    )
    executor = ProfilePaperExecutor(
        args.db, run_id=run_id,
        profile=profile, bankroll=backfill_bankroll, fill_model=fill_model,
    )
    dispatcher = Dispatcher([executor])

    # Iterate in chronological order.
    flags_written = 0
    positions_opened = 0
    for i, t in enumerate(above_floor, start=1):
        fid = await _write_cohort_copy_flag(args.db, t)
        if fid is None:
            continue
        flags_written += 1
        result = await dispatcher.on_flag(fid)
        if result.get(run_id) is not None:
            positions_opened += 1
        if i % 100 == 0:
            print(f"  ... {i}/{len(above_floor)} processed, "
                  f"{positions_opened} positions opened so far")

    print()
    print(f"flags_written:    {flags_written}")
    print(f"positions_opened: {positions_opened}")

    # P&L: settle resolved-market positions to truth, mark open positions.
    async with aiosqlite.connect(str(args.db)) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            """
            SELECT p.id, p.market_id, p.outcome, p.size_shares,
                   p.avg_entry_price_cents, m.resolved_outcome
            FROM paper_positions p
            JOIN markets m ON m.id = p.market_id
            WHERE p.run_id = ?
            """,
            (run_id,),
        ) as cur:
            rows = list(await cur.fetchall())

    resolved = [r for r in rows if r["resolved_outcome"]]
    unresolved = [r for r in rows if not r["resolved_outcome"]]

    realized_pnl_cents = 0
    wins = 0
    for r in resolved:
        size = int(r["size_shares"])
        entry = int(r["avg_entry_price_cents"])
        won = (str(r["outcome"]).upper() == str(r["resolved_outcome"]).upper())
        # YES that resolves YES pays $1.00 (100c) per share; otherwise $0.
        proceeds = size * (100 if won else 0)
        cost = size * entry
        pnl = proceeds - cost
        realized_pnl_cents += pnl
        if pnl > 0:
            wins += 1

    print()
    print("=== Backfill P&L ===")
    print(f"positions on resolved markets: {len(resolved)}")
    print(f"  wins:    {wins}")
    print(f"  losses:  {len(resolved) - wins}")
    print(f"  realized P&L: ${realized_pnl_cents/100:+,.2f}")
    print(f"positions still open:           {len(unresolved)}")
    if resolved:
        wr = wins / len(resolved) * 100
        print(f"  resolved win rate: {wr:.1f}%")
    print()
    print(f"backfill run id: #{run_id}  "
          f"(view: polysim run status --id {run_id})")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
