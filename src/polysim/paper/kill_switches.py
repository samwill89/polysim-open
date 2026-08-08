"""Kill switches — spec §14 #7 / build plan §4.5.

Three switches:
  1. Daily drawdown > max_drawdown_pct of starting balance
     -> pause run, require manual resume.
  2. > kill_flags_per_hour flag rate (1h rolling) -> pause run.
  3. Detector returning NaN / Inf -> already quarantined in the scorer.

Each switch is a pure predicate that returns a `TripReason` iff tripped.
Callers compose them and, on a trip, call `dao.pause_paper_run(reason)`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from polysim.db import dao
from polysim.utils.time import iso, now_utc


@dataclass(frozen=True)
class TripReason:
    code: str      # "drawdown" / "flag_rate"
    detail: str    # human-readable context


async def drawdown_switch(
    db_path: Path,
    run_id: int,
    *,
    max_drawdown_pct: float = 0.20,
) -> TripReason | None:
    """Trip when current balance has dropped more than `max_drawdown_pct`
    below the run's starting balance."""
    row = await dao.get_paper_run(db_path, run_id)
    if row is None:
        return None
    start = int(row.get("starting_balance_cents") or 0)
    current = int(row.get("current_balance_cents") or 0)
    if start <= 0:
        return None
    drawdown = (start - current) / start
    if drawdown > max_drawdown_pct:
        return TripReason(
            code="drawdown",
            detail=f"drawdown {drawdown:.1%} > limit {max_drawdown_pct:.1%}",
        )
    return None


async def flag_rate_switch(
    db_path: Path,
    *,
    max_flags_per_hour: int = 100,
    window: timedelta = timedelta(hours=1),
) -> TripReason | None:
    """Trip when more than `max_flags_per_hour` flags fired in the window."""
    since = now_utc() - window
    count = await dao.count_flags_since(db_path, since_iso=iso(since))
    if count > max_flags_per_hour:
        return TripReason(
            code="flag_rate",
            detail=f"{count} flags in the last {window} > limit {max_flags_per_hour}",
        )
    return None


async def check_and_pause(
    db_path: Path,
    run_id: int,
    *,
    max_drawdown_pct: float = 0.20,
    max_flags_per_hour: int = 100,
) -> TripReason | None:
    """Composite check — if any switch trips, pause the run and return the reason."""
    for check in (
        await drawdown_switch(db_path, run_id, max_drawdown_pct=max_drawdown_pct),
        await flag_rate_switch(db_path, max_flags_per_hour=max_flags_per_hour),
    ):
        if check is not None:
            await dao.pause_paper_run(
                db_path, run_id, reason=f"{check.code}: {check.detail}"
            )
            return check
    return None
