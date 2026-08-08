"""Structured belief schema for the Tier-2 investigator.

Empirical-priors addendum §5.2. Adopted from Opus 4.6's Kalshi belief log.
Every investigator call produces a `Belief`; every trading cycle produces
a `CyclePlan`; plans-of-record are `LongTermPlan`.

Semver'd. `SCHEMA_VERSION` bumps:
  - MAJOR: field removed or renamed (breaking)
  - MINOR: field added (non-breaking)
  - PATCH: constraint/enum relaxation (non-breaking)

Log artifacts live under `logs/beliefs/`, `logs/plans/cycle/`,
`logs/plans/long_term/`. Every row is schema-validated via
`validate_log_file()` — CI runs this against every .jsonl on PR.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

SCHEMA_VERSION = "1.0.0"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# ── Belief ──────────────────────────────────────────────


BeliefCategory = Literal[
    "event_analysis",
    "risk_assessment",
    "trading_strategy",
    "market_structure",
]


class Belief(_Frozen):
    """One confidence-scored statement about a market.

    Categories mirror Opus 4.6's log (§5.2). Every investigator call
    produces one or more Belief rows, written as JSONL to
    `logs/beliefs/<YYYY-MM-DD>.jsonl`.
    """

    market_id: str
    category: BeliefCategory
    confidence: float = Field(ge=0.0, le=1.0)
    estimated_true_probability: float = Field(ge=0.0, le=1.0)
    resolution_risk_score: float = Field(ge=0.0, le=1.0)
    expected_value_per_contract: float
    rationale: str  # ≤ 1 sentence — not enforced here; prompt guides it
    cohort_wallets_involved: list[str] = Field(default_factory=list)
    schema_version: str = SCHEMA_VERSION
    timestamp: datetime


# ── Plans ───────────────────────────────────────────────


class PortfolioSnapshot(_Frozen):
    """Thin view of the operator's paper portfolio at cycle start."""

    run_id: int
    balance_cents: int
    open_positions: int
    open_notional_cents: int
    settlement_pending_cents: int = 0
    drawdown_pct: float = 0.0


class TradeIntent(_Frozen):
    """A candidate trade the cycle wants to execute, in EV-rank order."""

    market_id: str
    outcome: Literal["YES", "NO"]
    intended_size_shares: int
    intended_price_cents: int
    expected_value_per_contract: float
    source_flag_id: int | None = None
    cohort_wallets: list[str] = Field(default_factory=list)


class CyclePlan(_Frozen):
    """One trading-loop cycle's plan.

    Written as a JSONL line to `logs/plans/cycle/<YYYY-MM-DD>.jsonl`.
    """

    cycle_id: str
    portfolio_snapshot: PortfolioSnapshot
    ready_trades: list[TradeIntent] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    opportunity_cost_estimate_cents: int = 0
    priority_actions: list[str] = Field(default_factory=list)
    schema_version: str = SCHEMA_VERSION
    created_at: datetime


class Phase(_Frozen):
    """One phase in a long-term plan."""

    name: str
    starts_on: date
    ends_on: date
    goals: list[str] = Field(default_factory=list)


class LongTermPlan(_Frozen):
    """Weekly/experiment-scope plan, persisted across cycles."""

    target_date: date
    phases: list[Phase] = Field(default_factory=list)
    lessons_learned: list[str] = Field(default_factory=list)
    schema_version: str = SCHEMA_VERSION
    created_at: datetime


# ── Log-file validator ──────────────────────────────────


_MODEL_BY_DIR: dict[str, type[BaseModel]] = {
    "beliefs": Belief,
    "cycle": CyclePlan,
    "long_term": LongTermPlan,
}


def _pick_model(path: Path) -> type[BaseModel] | None:
    """Route by parent-directory convention under logs/."""
    for part in path.parts:
        if part in _MODEL_BY_DIR:
            return _MODEL_BY_DIR[part]
    return None


def validate_log_file(path: Path) -> list[str]:
    """Validate every JSONL line in `path` against its expected schema.

    Returns a list of human-readable error strings; empty iff the file
    validates cleanly. Unknown files (no parent-dir match) return [].
    """
    model_cls = _pick_model(path)
    if model_cls is None:
        return []
    errors: list[str] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for lineno, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    raw: Any = json.loads(line)
                except json.JSONDecodeError as exc:
                    errors.append(f"{path}:{lineno}: bad JSON: {exc}")
                    continue
                try:
                    model_cls.model_validate(raw)
                except ValidationError as exc:
                    errors.append(f"{path}:{lineno}: {exc}")
    except OSError as exc:
        errors.append(f"{path}: {exc}")
    return errors


def validate_all_logs(root: Path = Path("logs")) -> list[str]:
    """Walk logs/ and validate every JSONL under schema-owned directories."""
    errors: list[str] = []
    if not root.exists():
        return errors
    for p in sorted(root.rglob("*.jsonl")):
        errors.extend(validate_log_file(p))
    return errors


def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Validate logs/ JSONL files against belief schema"
    )
    ap.add_argument(
        "--validate-all-logs", action="store_true",
        help="Walk logs/ and validate every known .jsonl",
    )
    ap.add_argument(
        "--path", type=Path, default=None,
        help="Validate a single file instead",
    )
    ap.add_argument(
        "--root", type=Path, default=Path("logs"),
        help="Root log directory (default: logs/)",
    )
    args = ap.parse_args(argv)

    errors = (
        validate_log_file(args.path)
        if args.path is not None
        else validate_all_logs(args.root)
    )
    if errors:
        for e in errors[:50]:
            sys.stdout.write(e + "\n")
        if len(errors) > 50:
            sys.stdout.write(f"... ({len(errors) - 50} more)\n")
        return 1
    sys.stdout.write(f"OK — schema {SCHEMA_VERSION}, no errors\n")
    return 0


if __name__ == "__main__":
    sys.exit(_main())


__all__ = [
    "SCHEMA_VERSION",
    "Belief",
    "BeliefCategory",
    "CyclePlan",
    "LongTermPlan",
    "Phase",
    "PortfolioSnapshot",
    "TradeIntent",
    "validate_all_logs",
    "validate_log_file",
]
