"""Build per-category late-activity base rates for `TimingDetector`.

G5 / build plan §2.7. Walks every resolved market, counts what % of total
shares traded fell in the last `--window-hours` before resolution, then
aggregates by `markets.category`. The result is written to the `meta`
table under key `timing_baselines` as `{category: float}`.

The TimingDetector picks these up at construction time (see
`evaluator/backtest._build_detectors`).

Usage:
    python scripts/build_timing_baselines.py --db polysim.db --window-hours 1
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import aiosqlite

from polysim.db import dao


async def compute_baselines(
    db_path: Path, *, window_hours: int = 1, min_markets_per_cat: int = 5
) -> dict[str, float]:
    """Returns {category: avg late-share-fraction across resolved markets}."""
    if not db_path.exists():
        return {}

    cutoff_h = window_hours
    by_cat_late: dict[str, list[float]] = defaultdict(list)

    async with aiosqlite.connect(str(db_path)) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT id, category, resolves_at FROM markets
            WHERE resolved_outcome IS NOT NULL AND resolves_at IS NOT NULL
            """
        ) as cur:
            markets = list(await cur.fetchall())

        for m in markets:
            cat = str(m["category"] or "unknown")
            try:
                resolves_at = datetime.fromisoformat(str(m["resolves_at"]))
            except ValueError:
                continue
            late_cutoff = (resolves_at - timedelta(hours=cutoff_h)).isoformat()

            async with db.execute(
                "SELECT COALESCE(SUM(size_shares), 0) FROM trades WHERE market_id = ?",
                (m["id"],),
            ) as cur1:
                total_row = await cur1.fetchone()
            total = int(total_row[0]) if total_row and total_row[0] is not None else 0
            if total <= 0:
                continue
            async with db.execute(
                "SELECT COALESCE(SUM(size_shares), 0) FROM trades "
                "WHERE market_id = ? AND timestamp >= ?",
                (m["id"], late_cutoff),
            ) as cur2:
                late_row = await cur2.fetchone()
            late = int(late_row[0]) if late_row and late_row[0] is not None else 0
            by_cat_late[cat].append(late / total)

    baselines: dict[str, float] = {}
    for cat, fracs in by_cat_late.items():
        if len(fracs) < min_markets_per_cat:
            continue
        baselines[cat] = sum(fracs) / len(fracs)
    return baselines


async def _amain(args: argparse.Namespace) -> int:
    baselines = await compute_baselines(
        Path(args.db),
        window_hours=args.window_hours,
        min_markets_per_cat=args.min_markets,
    )
    if not baselines:
        print(
            f"no resolved markets with >= {args.min_markets} samples per category yet — "
            "TimingDetector will keep using the 0.20 default."
        )
        return 0

    if args.print_only:
        print(json.dumps(baselines, indent=2))
        return 0

    await dao.meta_set_json(
        Path(args.db),
        key="timing_baselines",
        value=baselines,
    )
    print(f"wrote {len(baselines)} category baselines to meta table:")
    for cat, val in sorted(baselines.items(), key=lambda kv: -kv[1]):
        print(f"  {cat:<18} {val:.3f}")
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="Build per-category timing baselines")
    p.add_argument("--db", default="polysim.db")
    p.add_argument("--window-hours", type=int, default=1)
    p.add_argument(
        "--min-markets", type=int, default=5,
        help="Minimum resolved markets in a category to emit a baseline.",
    )
    p.add_argument(
        "--print-only", action="store_true",
        help="Print JSON baselines to stdout without writing to the meta table.",
    )
    args = p.parse_args()
    sys.exit(asyncio.run(_amain(args)))


if __name__ == "__main__":
    main()
