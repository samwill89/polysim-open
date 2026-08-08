"""Replay a historical time window — Phase 1 / build plan §1.

Thin wrapper that delegates to `polysim.evaluator.backtest.run_backtest`.
The same logic is exposed via `polysim replay`; this script exists for
operators who prefer a script invocation.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

from polysim.config import load_config
from polysim.evaluator.backtest import run_backtest


def _parse_date(s: str) -> datetime:
    if "T" in s:
        return datetime.fromisoformat(s).astimezone(UTC)
    return datetime.fromisoformat(s).replace(tzinfo=UTC)


def main() -> None:
    parser = argparse.ArgumentParser(description="PolySim historical replay")
    parser.add_argument("--from", dest="frm", required=True, help="YYYY-MM-DD or ISO")
    parser.add_argument("--to", dest="to", required=True)
    parser.add_argument("--db", default="polysim.db")
    parser.add_argument("--config", default="config.yml")
    parser.add_argument("--profile", default="systematic")
    parser.add_argument("--no-run", action="store_true", help="skip the paper run pass")
    args = parser.parse_args()

    cfg = load_config(Path(args.config))
    start = _parse_date(args.frm)
    end = _parse_date(args.to)
    out = asyncio.run(
        run_backtest(
            start=start, end=end,
            db_path=Path(args.db), cfg=cfg,
            open_paper_run=not args.no_run,
            profile_name=args.profile,
        )
    )
    print(f"trades={out['trades_seen']}  flags={out['flags_created']}  "
          f"run={out.get('run_id')}  positions={out['positions_opened']}")
    sys.exit(0)


if __name__ == "__main__":
    main()
