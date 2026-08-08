"""Cycle + long-term planning — empirical-priors addendum §5.2.

Pure-Python builders for `CyclePlan` and `LongTermPlan`. The trading
loop calls `build_cycle_plan()` once per tick; weekly cron writes a
`LongTermPlan`. Both write JSONL artifacts to logs/plans/ for
end-of-experiment review.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, date, datetime
from pathlib import Path

from polysim.agents.belief_schema import (
    SCHEMA_VERSION,
    CyclePlan,
    LongTermPlan,
    Phase,
    PortfolioSnapshot,
    TradeIntent,
)

log = logging.getLogger(__name__)

CYCLE_LOG_DIR = Path("logs/plans/cycle")
LONG_TERM_LOG_DIR = Path("logs/plans/long_term")


def make_cycle_id(prefix: str = "cyc") -> str:
    """Stable cycle id for grouping rejections + beliefs in one tick."""
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def build_cycle_plan(
    *,
    portfolio: PortfolioSnapshot,
    ready_trades: list[TradeIntent],
    blockers: list[str] | None = None,
    opportunity_cost_estimate_cents: int = 0,
    priority_actions: list[str] | None = None,
    cycle_id: str | None = None,
) -> CyclePlan:
    return CyclePlan(
        cycle_id=cycle_id or make_cycle_id(),
        portfolio_snapshot=portfolio,
        ready_trades=ready_trades,
        blockers=blockers or [],
        opportunity_cost_estimate_cents=opportunity_cost_estimate_cents,
        priority_actions=priority_actions or [],
        schema_version=SCHEMA_VERSION,
        created_at=datetime.now(UTC),
    )


def build_long_term_plan(
    *,
    target_date: date,
    phases: list[Phase],
    lessons_learned: list[str] | None = None,
) -> LongTermPlan:
    return LongTermPlan(
        target_date=target_date,
        phases=phases,
        lessons_learned=lessons_learned or [],
        schema_version=SCHEMA_VERSION,
        created_at=datetime.now(UTC),
    )


def append_cycle_plan(plan: CyclePlan) -> Path:
    CYCLE_LOG_DIR.mkdir(parents=True, exist_ok=True)
    target = CYCLE_LOG_DIR / f"{plan.created_at.astimezone(UTC).date().isoformat()}.jsonl"
    with target.open("a", encoding="utf-8") as f:
        f.write(plan.model_dump_json() + "\n")
    return target


def append_long_term_plan(plan: LongTermPlan) -> Path:
    LONG_TERM_LOG_DIR.mkdir(parents=True, exist_ok=True)
    target = LONG_TERM_LOG_DIR / f"{plan.created_at.astimezone(UTC).date().isoformat()}.jsonl"
    with target.open("a", encoding="utf-8") as f:
        f.write(plan.model_dump_json() + "\n")
    return target


__all__ = [
    "append_cycle_plan",
    "append_long_term_plan",
    "build_cycle_plan",
    "build_long_term_plan",
    "make_cycle_id",
]
