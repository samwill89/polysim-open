"""Six-gate decision module tests — empirical-priors addendum §5.4.

Including the H6 mechanism: directional inconsistency must veto the
trade even when cohort wallets are loud, even in degen mode.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from polysim.agents.belief_schema import Belief
from polysim.db.migrations.runner import apply_migrations
from polysim.trading.decision_gate import (
    evaluate_gates,
    gate_results_to_summary,
    log_rejection,
)


def _belief(
    *, confidence: float = 0.7,
    p_yes: float = 0.7,
    res_risk: float = 0.1,
    ev: float = 0.5,
    market_id: str = "m1",
) -> Belief:
    return Belief(
        market_id=market_id, category="event_analysis",
        confidence=confidence,
        estimated_true_probability=p_yes,
        resolution_risk_score=res_risk,
        expected_value_per_contract=ev,
        rationale="t",
        cohort_wallets_involved=["0xaf4"],
        timestamp=datetime.now(UTC),
    )


def test_all_gates_pass_for_clean_signal() -> None:
    res = evaluate_gates(
        belief=_belief(),
        cohort_side="YES",
        spread_cost_per_contract=0.04,
        depth_ok=True, concentration_ok=True,
        mode="systematic",
    )
    assert res.overall_passed is True
    assert all(o.passed for o in res.outcomes)


def test_low_confidence_vetoes() -> None:
    res = evaluate_gates(
        belief=_belief(confidence=0.5),
        cohort_side="YES",
        spread_cost_per_contract=0.04,
        depth_ok=True, concentration_ok=True,
    )
    assert res.overall_passed is False
    f = res.first_failure()
    assert f is not None
    assert f.name == "a_confidence"


def test_high_resolution_risk_vetoes_systematic_but_passes_degen() -> None:
    b = _belief(res_risk=0.5)
    sys_res = evaluate_gates(
        belief=b, cohort_side="YES",
        spread_cost_per_contract=0.04,
        depth_ok=True, concentration_ok=True, mode="systematic",
    )
    deg_res = evaluate_gates(
        belief=b, cohort_side="YES",
        spread_cost_per_contract=0.04,
        depth_ok=True, concentration_ok=True, mode="degen",
    )
    assert sys_res.overall_passed is False
    assert next(o for o in sys_res.outcomes if o.name == "b_resolution_risk").passed is False
    assert deg_res.overall_passed is True


def test_ev_below_spread_vetoes_systematic() -> None:
    res = evaluate_gates(
        belief=_belief(ev=0.02),
        cohort_side="YES",
        spread_cost_per_contract=0.04,
        depth_ok=True, concentration_ok=True,
    )
    assert res.overall_passed is False
    f = res.first_failure()
    assert f is not None
    assert "ev_vs_spread" in f.name


def test_directional_inconsistency_vetoes_in_all_modes() -> None:
    """H6 mechanism: cohort YES, investigator says P(YES) < 0.5 → VETO."""
    b = _belief(confidence=0.9, p_yes=0.30, res_risk=0.0, ev=10.0)
    for mode in ("systematic", "degen"):
        res = evaluate_gates(
            belief=b, cohort_side="YES",
            spread_cost_per_contract=0.04,
            depth_ok=True, concentration_ok=True,
            mode=mode,  # type: ignore[arg-type]
        )
        assert res.overall_passed is False
        d = next(o for o in res.outcomes if o.name == "d_directional")
        assert d.passed is False
        assert "directional inconsistency" in d.reason


def test_depth_failure_vetoes() -> None:
    res = evaluate_gates(
        belief=_belief(),
        cohort_side="YES",
        spread_cost_per_contract=0.04,
        depth_ok=False, concentration_ok=True,
    )
    assert res.overall_passed is False
    e = next(o for o in res.outcomes if o.name == "e_depth")
    assert e.passed is False


def test_concentration_relaxed_in_degen() -> None:
    sys_res = evaluate_gates(
        belief=_belief(),
        cohort_side="YES", spread_cost_per_contract=0.04,
        depth_ok=True, concentration_ok=False, mode="systematic",
    )
    deg_res = evaluate_gates(
        belief=_belief(),
        cohort_side="YES", spread_cost_per_contract=0.04,
        depth_ok=True, concentration_ok=False, mode="degen",
    )
    assert sys_res.overall_passed is False
    assert deg_res.overall_passed is True


def test_summary_rolls_up_first_failures() -> None:
    losers = [
        evaluate_gates(
            belief=_belief(confidence=0.4),
            cohort_side="YES", spread_cost_per_contract=0.04,
            depth_ok=True, concentration_ok=True,
        ),
        evaluate_gates(
            belief=_belief(confidence=0.4),
            cohort_side="YES", spread_cost_per_contract=0.04,
            depth_ok=True, concentration_ok=True,
        ),
        evaluate_gates(
            belief=_belief(p_yes=0.30),
            cohort_side="YES", spread_cost_per_contract=0.04,
            depth_ok=True, concentration_ok=True,
        ),
    ]
    s = gate_results_to_summary(losers)
    assert s["total"] == 3
    assert s["rejected"] == 3
    assert s["first_fail_a_confidence"] == 2
    assert s["first_fail_d_directional"] == 1


@pytest.fixture
async def db(tmp_path: Path) -> Path:
    p = tmp_path / "t.db"
    await apply_migrations(p)
    return p


async def test_log_rejection_writes_jsonl_and_db(db: Path, tmp_path: Path) -> None:
    res = evaluate_gates(
        belief=_belief(confidence=0.4),
        cohort_side="YES", spread_cost_per_contract=0.04,
        depth_ok=True, concentration_ok=True,
        cycle_id="cyc-test",
    )
    rid = await log_rejection(
        db, result=res, belief=_belief(confidence=0.4),
        flag_id=None, write_jsonl=False,
    )
    assert rid is not None and rid > 0
    import aiosqlite
    async with aiosqlite.connect(str(db)) as conn, conn.execute(
        "SELECT cycle_id, market_id, gate_confidence, rejection_reason "
        "FROM decision_rejections WHERE id = ?", (rid,),
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    assert row[0] == "cyc-test"
    assert row[1] == "m1"
    assert row[2] == 0  # confidence gate failed
    assert "confidence" in row[3]
