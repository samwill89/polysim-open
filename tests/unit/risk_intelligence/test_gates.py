from __future__ import annotations

from datetime import UTC, datetime

from polysim.risk_intelligence.gates import (
    CorrelationContext,
    evaluate_trade_risk,
)
from polysim.risk_intelligence.schema import EvidenceAssessment


def _assessment(**updates: object) -> EvidenceAssessment:
    base: dict[str, object] = {
        "market_id": "m1",
        "subject_key": "mitch mcconnell",
        "assessed_at": datetime(2026, 7, 10, tzinfo=UTC),
        "provider": "analyst",
        "assessment_kind": "analyst",
        "catalyst_sensitive": True,
        "status": "corroborated",
        "source_count": 4,
        "independent_domain_count": 4,
        "primary_source_count": 1,
        "high_quality_source_count": 3,
        "rumor_article_count": 1,
        "source_quality_score": 0.85,
        "rumor_risk_score": 0.2,
        "information_asymmetry_score": 0.25,
        "fair_probability_yes": 0.70,
        "probability_confidence": 0.80,
        "summary": "corroborated",
    }
    base.update(updates)
    return EvidenceAssessment.model_validate(base)


def _evaluate(
    *,
    assessment: EvidenceAssessment,
    correlation: CorrelationContext | None = None,
) -> object:
    return evaluate_trade_risk(
        policy="require_verified_edge",
        assessment=assessment,
        correlation=correlation or CorrelationContext("mitch mcconnell"),
        outcome="YES",
        shares=100,
        entry_price_cents=50,
        fee_cents=100,
        spread_cents=2,
        starting_balance_cents=1_000_000,
        max_correlated_positions=1,
        max_subject_exposure_pct=0.02,
        min_source_quality=0.60,
        max_rumor_risk=0.50,
        max_information_asymmetry=0.65,
        min_probability_confidence=0.65,
        min_edge_after_cost_cents=2.0,
        uncertainty_penalty_max_cents=10.0,
    )


def test_sensitive_market_without_probability_is_rejected() -> None:
    result = _evaluate(assessment=_assessment(fair_probability_yes=None))
    assert result.passed is False
    assert result.gate_name == "probability_missing"


def test_rumor_heavy_market_is_rejected_before_edge_math() -> None:
    result = _evaluate(
        assessment=_assessment(
            status="rumor_heavy",
            source_quality_score=0.2,
            rumor_risk_score=0.8,
        )
    )
    assert result.passed is False
    assert result.gate_name == "evidence_quality"


def test_verified_probability_must_clear_fill_fee_and_uncertainty() -> None:
    result = _evaluate(assessment=_assessment())
    assert result.passed is True
    assert result.gate_name == "verified_edge"
    assert result.edge_after_cost_cents == 17.0


def test_same_subject_second_position_is_rejected() -> None:
    result = _evaluate(
        assessment=_assessment(),
        correlation=CorrelationContext(
            "mitch mcconnell",
            open_positions=1,
            exposure_cents=2_340,
            position_ids=(1100,),
        ),
    )
    assert result.passed is False
    assert result.gate_name == "correlation"
    assert "1100" in str(result.detail)


def test_monitor_policy_records_would_block_but_passes() -> None:
    assessment = _assessment(fair_probability_yes=None)
    result = evaluate_trade_risk(
        policy="monitor",
        assessment=assessment,
        correlation=CorrelationContext("mitch mcconnell"),
        outcome="YES",
        shares=10,
        entry_price_cents=50,
        fee_cents=10,
        spread_cents=2,
        starting_balance_cents=1_000_000,
        max_correlated_positions=1,
        max_subject_exposure_pct=0.02,
        min_source_quality=0.6,
        max_rumor_risk=0.5,
        max_information_asymmetry=0.65,
        min_probability_confidence=0.65,
        min_edge_after_cost_cents=2.0,
        uncertainty_penalty_max_cents=10.0,
    )
    assert result.passed is True
    assert result.gate_name == "monitor:probability_missing"
