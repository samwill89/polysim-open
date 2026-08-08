"""Pure evidence, correlation, and executable-edge gates."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from polysim.risk_intelligence.schema import EvidenceAssessment

RiskPolicy = Literal["off", "monitor", "require_verified_edge"]


@dataclass(frozen=True)
class CorrelationContext:
    subject_key: str | None
    open_positions: int = 0
    exposure_cents: int = 0
    position_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class RiskDecision:
    passed: bool
    gate_name: str
    reason: str
    policy: RiskPolicy
    subject_key: str | None
    catalyst_sensitive: bool
    evidence_status: str | None
    correlated_positions: int
    subject_exposure_cents: int
    entry_price_cents: int
    fee_cents: int
    spread_cents: int | None
    fair_value_cents: float | None
    uncertainty_penalty_cents: float
    edge_after_cost_cents: float | None
    detail: dict[str, object]


def _decision(
    *,
    passed: bool,
    gate_name: str,
    reason: str,
    policy: RiskPolicy,
    assessment: EvidenceAssessment | None,
    correlation: CorrelationContext,
    entry_price_cents: int,
    fee_cents: int,
    spread_cents: int | None,
    fair_value_cents: float | None = None,
    uncertainty_penalty_cents: float = 0.0,
    edge_after_cost_cents: float | None = None,
    detail: dict[str, object] | None = None,
) -> RiskDecision:
    return RiskDecision(
        passed=passed,
        gate_name=gate_name,
        reason=reason,
        policy=policy,
        subject_key=correlation.subject_key,
        catalyst_sensitive=bool(assessment and assessment.catalyst_sensitive),
        evidence_status=(assessment.status if assessment else None),
        correlated_positions=correlation.open_positions,
        subject_exposure_cents=correlation.exposure_cents,
        entry_price_cents=entry_price_cents,
        fee_cents=fee_cents,
        spread_cents=spread_cents,
        fair_value_cents=fair_value_cents,
        uncertainty_penalty_cents=uncertainty_penalty_cents,
        edge_after_cost_cents=edge_after_cost_cents,
        detail=detail or {},
    )


def evaluate_trade_risk(
    *,
    policy: RiskPolicy,
    assessment: EvidenceAssessment | None,
    correlation: CorrelationContext,
    outcome: str,
    shares: int,
    entry_price_cents: int,
    fee_cents: int,
    spread_cents: int | None,
    starting_balance_cents: int,
    max_correlated_positions: int,
    max_subject_exposure_pct: float,
    min_source_quality: float,
    max_rumor_risk: float,
    max_information_asymmetry: float,
    min_probability_confidence: float,
    min_edge_after_cost_cents: float,
    uncertainty_penalty_max_cents: float,
) -> RiskDecision:
    """Evaluate a candidate at its simulated executable fill."""
    if policy == "off":
        return _decision(
            passed=True,
            gate_name="off",
            reason="risk intelligence disabled",
            policy=policy,
            assessment=assessment,
            correlation=correlation,
            entry_price_cents=entry_price_cents,
            fee_cents=fee_cents,
            spread_cents=spread_cents,
        )

    monitor_only = policy == "monitor"
    candidate_notional = max(0, shares * entry_price_cents)
    max_subject_cents = int(max(0, starting_balance_cents) * max_subject_exposure_pct)

    if correlation.subject_key and correlation.open_positions >= max_correlated_positions:
        result = _decision(
            passed=False,
            gate_name="correlation",
            reason=(
                f"subject {correlation.subject_key!r} already has "
                f"{correlation.open_positions} open position(s)"
            ),
            policy=policy,
            assessment=assessment,
            correlation=correlation,
            entry_price_cents=entry_price_cents,
            fee_cents=fee_cents,
            spread_cents=spread_cents,
            detail={"position_ids": list(correlation.position_ids)},
        )
        return result if not monitor_only else _monitor_pass(result)

    if (
        correlation.subject_key
        and max_subject_cents > 0
        and correlation.exposure_cents + candidate_notional > max_subject_cents
    ):
        result = _decision(
            passed=False,
            gate_name="subject_exposure",
            reason=(
                f"subject exposure {correlation.exposure_cents + candidate_notional} "
                f"> cap {max_subject_cents} cents"
            ),
            policy=policy,
            assessment=assessment,
            correlation=correlation,
            entry_price_cents=entry_price_cents,
            fee_cents=fee_cents,
            spread_cents=spread_cents,
        )
        return result if not monitor_only else _monitor_pass(result)

    if assessment is None or not assessment.catalyst_sensitive:
        return _decision(
            passed=True,
            gate_name="not_sensitive",
            reason="no catalyst-sensitive evidence gate required",
            policy=policy,
            assessment=assessment,
            correlation=correlation,
            entry_price_cents=entry_price_cents,
            fee_cents=fee_cents,
            spread_cents=spread_cents,
        )

    evidence_failures: list[str] = []
    if assessment.status in {"insufficient", "rumor_heavy"}:
        evidence_failures.append(f"status={assessment.status}")
    if assessment.source_quality_score < min_source_quality:
        evidence_failures.append(f"source_quality={assessment.source_quality_score:.2f}")
    if assessment.rumor_risk_score > max_rumor_risk:
        evidence_failures.append(f"rumor_risk={assessment.rumor_risk_score:.2f}")
    if assessment.information_asymmetry_score > max_information_asymmetry:
        evidence_failures.append(
            f"information_asymmetry={assessment.information_asymmetry_score:.2f}"
        )
    if evidence_failures:
        result = _decision(
            passed=False,
            gate_name="evidence_quality",
            reason="; ".join(evidence_failures),
            policy=policy,
            assessment=assessment,
            correlation=correlation,
            entry_price_cents=entry_price_cents,
            fee_cents=fee_cents,
            spread_cents=spread_cents,
        )
        return result if not monitor_only else _monitor_pass(result)

    if assessment.fair_probability_yes is None:
        result = _decision(
            passed=False,
            gate_name="probability_missing",
            reason="sensitive market has no analyst fair probability",
            policy=policy,
            assessment=assessment,
            correlation=correlation,
            entry_price_cents=entry_price_cents,
            fee_cents=fee_cents,
            spread_cents=spread_cents,
        )
        return result if not monitor_only else _monitor_pass(result)
    if assessment.probability_confidence < min_probability_confidence:
        result = _decision(
            passed=False,
            gate_name="probability_confidence",
            reason=(
                f"probability confidence {assessment.probability_confidence:.2f} "
                f"< {min_probability_confidence:.2f}"
            ),
            policy=policy,
            assessment=assessment,
            correlation=correlation,
            entry_price_cents=entry_price_cents,
            fee_cents=fee_cents,
            spread_cents=spread_cents,
        )
        return result if not monitor_only else _monitor_pass(result)

    p_yes = assessment.fair_probability_yes
    fair_value = 100.0 * (p_yes if outcome.strip().upper() == "YES" else 1.0 - p_yes)
    fee_per_share = fee_cents / max(1, shares)
    uncertainty = (1.0 - assessment.probability_confidence) * uncertainty_penalty_max_cents
    edge = fair_value - entry_price_cents - fee_per_share - uncertainty
    if edge < min_edge_after_cost_cents:
        result = _decision(
            passed=False,
            gate_name="edge_after_cost",
            reason=(
                f"edge after executable price, fee, and uncertainty "
                f"{edge:.2f}c < {min_edge_after_cost_cents:.2f}c"
            ),
            policy=policy,
            assessment=assessment,
            correlation=correlation,
            entry_price_cents=entry_price_cents,
            fee_cents=fee_cents,
            spread_cents=spread_cents,
            fair_value_cents=fair_value,
            uncertainty_penalty_cents=uncertainty,
            edge_after_cost_cents=edge,
        )
        return result if not monitor_only else _monitor_pass(result)

    return _decision(
        passed=True,
        gate_name="verified_edge",
        reason=f"verified edge {edge:.2f}c",
        policy=policy,
        assessment=assessment,
        correlation=correlation,
        entry_price_cents=entry_price_cents,
        fee_cents=fee_cents,
        spread_cents=spread_cents,
        fair_value_cents=fair_value,
        uncertainty_penalty_cents=uncertainty,
        edge_after_cost_cents=edge,
    )


def _monitor_pass(result: RiskDecision) -> RiskDecision:
    return RiskDecision(
        **{
            **result.__dict__,
            "passed": True,
            "gate_name": f"monitor:{result.gate_name}",
            "reason": f"monitor only - would block: {result.reason}",
        }
    )


__all__ = [
    "CorrelationContext",
    "RiskDecision",
    "RiskPolicy",
    "evaluate_trade_risk",
]
