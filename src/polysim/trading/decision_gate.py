"""Six-gate decision module — empirical-priors addendum §5.4.

For every candidate trade:
    1. Wallet signal detected → cohort_signal_strength
    2. Investigator invoked → Belief
    3. Six gates evaluated
    4. ALL must pass (systematic) → trade; ANY fails → reject + log

Gates (systematic mode):
    a) belief.confidence >= 0.6
    b) belief.resolution_risk_score <= 0.3
    c) belief.expected_value_per_contract > spread_cost_per_contract
    d) directional consistency: belief direction matches cohort side
    e) market depth check (delegated to portfolio.depth)
    f) concentration: <=2 positions per niche, <=15% per geopolitical theme

Degen mode relaxes (b), (c), (f) but keeps (a) and (d). The directional-
inconsistency veto (d) is non-negotiable — we never trade against our
own best estimate of true probability.

Rejection logging is load-bearing: every reject writes a JSONL line to
`logs/decisions/rejections.jsonl` AND a row to `decision_rejections`.
At end-of-experiment, the H6 hypothesis test reconstructs counterfactual
P&L for every rejected trade.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import aiosqlite

from polysim.agents.belief_schema import Belief
from polysim.utils.time import iso, now_utc

log = logging.getLogger(__name__)

LOGS_DIR = Path("logs/decisions")
REJECTIONS_PATH = LOGS_DIR / "rejections.jsonl"

GateMode = Literal["systematic", "medium", "degen"]


@dataclass(frozen=True)
class GateOutcome:
    """One gate's verdict + reason."""

    name: str                              # 'a_confidence' | 'b_resolution_risk' | ...
    passed: bool
    reason: str = ""
    detail: dict[str, Any] | None = None


@dataclass(frozen=True)
class GateResult:
    """All-gates summary."""

    market_id: str
    cycle_id: str | None
    mode: GateMode
    outcomes: tuple[GateOutcome, ...]
    overall_passed: bool

    def first_failure(self) -> GateOutcome | None:
        for o in self.outcomes:
            if not o.passed:
                return o
        return None

    def reasons_summary(self) -> str:
        if self.overall_passed:
            return "all gates passed"
        first = self.first_failure()
        return f"{first.name}: {first.reason}" if first else "unknown"


def evaluate_gates(
    *,
    belief: Belief,
    cohort_side: str,                      # "YES" | "NO"
    spread_cost_per_contract: float,       # cents
    depth_ok: bool,
    concentration_ok: bool,
    mode: GateMode = "systematic",
    cycle_id: str | None = None,
    confidence_min: float = 0.6,
    resolution_risk_max_systematic: float = 0.3,
    resolution_risk_max_degen: float = 0.6,
) -> GateResult:
    """Pure: run all six gates and return a GateResult.

    The caller (executor) supplies the `depth_ok` and `concentration_ok`
    booleans because those checks already exist elsewhere in the codebase
    (portfolio.depth, paper.profile_executor) — we don't want this module
    to re-implement them.
    """
    outcomes: list[GateOutcome] = []

    # (a) confidence — applies in all modes
    a_pass = belief.confidence >= confidence_min
    outcomes.append(GateOutcome(
        name="a_confidence", passed=a_pass,
        reason="" if a_pass else (
            f"confidence {belief.confidence:.2f} < {confidence_min:.2f}"
        ),
        detail={"confidence": belief.confidence},
    ))

    # (b) resolution risk — relaxed in degen mode
    res_max = (
        resolution_risk_max_degen if mode == "degen"
        else resolution_risk_max_systematic
    )
    b_pass = belief.resolution_risk_score <= res_max
    outcomes.append(GateOutcome(
        name="b_resolution_risk", passed=b_pass,
        reason="" if b_pass else (
            f"resolution_risk_score {belief.resolution_risk_score:.2f} "
            f"> {res_max:.2f}"
        ),
        detail={
            "score": belief.resolution_risk_score,
            "limit": res_max,
        },
    ))

    # (c) EV vs spread — relaxed in degen mode (but still tracked)
    if mode == "degen":
        c_pass = belief.expected_value_per_contract > 0
        c_reason = "" if c_pass else (
            f"EV {belief.expected_value_per_contract:.3f} <= 0 "
            "(degen requires positive EV)"
        )
    else:
        c_pass = belief.expected_value_per_contract > spread_cost_per_contract
        c_reason = "" if c_pass else (
            f"EV {belief.expected_value_per_contract:.3f} <= spread cost "
            f"{spread_cost_per_contract:.3f}"
        )
    outcomes.append(GateOutcome(
        name="c_ev_vs_spread", passed=c_pass, reason=c_reason,
        detail={
            "ev": belief.expected_value_per_contract,
            "spread_cost": spread_cost_per_contract,
        },
    ))

    # (d) directional consistency — non-negotiable in all modes
    side = cohort_side.strip().upper()
    if side == "YES":
        d_pass = belief.estimated_true_probability >= 0.5
    elif side == "NO":
        d_pass = belief.estimated_true_probability < 0.5
    else:
        d_pass = False
    outcomes.append(GateOutcome(
        name="d_directional", passed=d_pass,
        reason="" if d_pass else (
            f"cohort side {side} but investigator estimates "
            f"P(YES)={belief.estimated_true_probability:.2f} "
            "(directional inconsistency veto)"
        ),
        detail={
            "cohort_side": side,
            "p_yes": belief.estimated_true_probability,
        },
    ))

    # (e) depth — delegated
    outcomes.append(GateOutcome(
        name="e_depth", passed=depth_ok,
        reason="" if depth_ok else "depth check failed (see executor logs)",
    ))

    # (f) concentration — relaxed in degen mode
    if mode == "degen":
        f_pass = True
        f_reason = ""
    else:
        f_pass = concentration_ok
        f_reason = "" if f_pass else "concentration cap hit"
    outcomes.append(GateOutcome(
        name="f_concentration", passed=f_pass, reason=f_reason,
    ))

    overall = all(o.passed for o in outcomes)
    return GateResult(
        market_id=belief.market_id,
        cycle_id=cycle_id,
        mode=mode,
        outcomes=tuple(outcomes),
        overall_passed=overall,
    )


async def log_rejection(
    db_path: Path,
    *,
    result: GateResult,
    belief: Belief,
    flag_id: int | None = None,
    write_jsonl: bool = True,
) -> int | None:
    """Persist rejection to JSONL + decision_rejections. Returns row id or None
    if the gate passed (no rejection to log).
    """
    if result.overall_passed:
        return None

    if write_jsonl:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        line = json.dumps({
            "rejected_at": iso(now_utc()),
            "market_id": result.market_id,
            "cycle_id": result.cycle_id,
            "mode": result.mode,
            "reason": result.reasons_summary(),
            "outcomes": [
                {"name": o.name, "passed": o.passed, "reason": o.reason}
                for o in result.outcomes
            ],
            "belief": belief.model_dump(mode="json"),
        }, default=str) + "\n"
        with REJECTIONS_PATH.open("a", encoding="utf-8") as f:
            f.write(line)

    by_name = {o.name: o.passed for o in result.outcomes}
    async with aiosqlite.connect(str(db_path)) as db:
        cur = await db.execute(
            """
            INSERT INTO decision_rejections(
                cycle_id, flag_id, market_id, belief_json,
                gate_confidence, gate_resolution_risk, gate_ev,
                gate_directional, gate_depth, gate_concentration,
                rejection_reason, rejected_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                result.cycle_id, flag_id, result.market_id,
                json.dumps(belief.model_dump(mode="json"), default=str),
                int(by_name.get("a_confidence", False)),
                int(by_name.get("b_resolution_risk", False)),
                int(by_name.get("c_ev_vs_spread", False)),
                int(by_name.get("d_directional", False)),
                int(by_name.get("e_depth", False)),
                int(by_name.get("f_concentration", False)),
                result.reasons_summary(),
                iso(now_utc()),
            ),
        )
        new_id = int(cur.lastrowid or 0)
        await cur.close()
        await db.commit()
    return new_id


def gate_results_to_summary(
    results: Iterable[GateResult],
) -> dict[str, int]:
    """Quick rollup for end-of-experiment reporting."""
    total = 0
    passed = 0
    by_first_fail: dict[str, int] = {}
    for r in results:
        total += 1
        if r.overall_passed:
            passed += 1
            continue
        first = r.first_failure()
        if first is None:
            continue
        by_first_fail[first.name] = by_first_fail.get(first.name, 0) + 1
    return {
        "total": total,
        "passed": passed,
        "rejected": total - passed,
        **{f"first_fail_{k}": v for k, v in by_first_fail.items()},
    }


__all__ = [
    "GateMode",
    "GateOutcome",
    "GateResult",
    "evaluate_gates",
    "gate_results_to_summary",
    "log_rejection",
]


