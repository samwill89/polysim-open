"""Golden-path tests — empirical-priors addendum §9.2.

These reproduce known historical scenarios with synthetic fixtures
shaped like the real ones, asserting our gates + valuations behave
correctly. The fixtures are intentionally synthetic — we don't ship
copyrighted Arcada / Opus-4.6 raw data — but the *shape* of each
scenario (concentration ratios, resolution-criteria specifics, bid-ask
spreads) is preserved.

  * test_opus_replay_concentration_and_resolution_risk
      — April 14-26 Israel-Lebanon shape: investigator's belief flags
        resolution_risk_score>0.3 on the April-19 deadline market;
        decision gate vetoes the trade.
  * test_arcada_bid_price_mtm_differs_from_mid
      — replay shape: a position bought at 50c with bid=48/ask=52
        values to 48c per share, NOT mid (50c). MTM gap matches Arcada's
        reported overstatement.
  * test_arb_detection_skips_yes_plus_no_below_one_dollar
      — feed correlated tokens with YES_ask + NO_ask = 96c → arb_detected.
  * test_decision_gate_directional_inconsistency_veto (delegates to
        the existing test in tests/unit/trading/test_decision_gate.py;
        we add a thin wrapper here for the §9.2 acceptance bundle).
  * test_niche_weighting_consensus_math
      — 5-wallet cohort: 2 niche x 1.5x weight + 3 general x 1.0x →
        verify the weighted signal_strength matches the spec formula.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from polysim.agents.belief_schema import Belief
from polysim.portfolio.valuation import value_position
from polysim.trading.arb_detector import check_arb_pattern
from polysim.trading.consensus import (
    GENERAL_WEIGHT,
    NICHE_WEIGHT,
    CohortWalletInMarket,
    aggregate_signal,
)
from polysim.trading.decision_gate import evaluate_gates

# ── Opus 4.6 shape ──────────────────────────────────────


def test_opus_replay_concentration_and_resolution_risk() -> None:
    """April 14-26 Israel-Lebanon shape: investigator's resolution_risk
    on the April-19 market should veto the trade in systematic mode."""
    belief = Belief(
        market_id="m_il_lebanon_apr19",
        category="risk_assessment",
        confidence=0.85,
        estimated_true_probability=0.65,             # investigator agrees with cohort YES
        resolution_risk_score=0.55,                  # ABOVE 0.3 systematic threshold
        expected_value_per_contract=2.0,
        rationale=(
            "Adjacent-day deadline ambiguity: April 14 meeting may count "
            "for Apr-26 + Apr-30 deadlines but not Apr-19."
        ),
        cohort_wallets_involved=["0xaf4"],
        timestamp=datetime.now(UTC),
    )
    res = evaluate_gates(
        belief=belief, cohort_side="YES",
        spread_cost_per_contract=0.04,
        depth_ok=True, concentration_ok=True,
        mode="systematic",
    )
    assert res.overall_passed is False
    failure = res.first_failure()
    assert failure is not None
    assert failure.name == "b_resolution_risk"
    assert "resolution_risk_score" in failure.reason


def test_opus_replay_resolution_risk_relaxed_in_degen() -> None:
    """Same scenario, degen mode: 0.55 < 0.6 cap, so the resolution-risk
    gate doesn't veto. Other gates still apply."""
    belief = Belief(
        market_id="m_il_lebanon_apr19",
        category="risk_assessment",
        confidence=0.85,
        estimated_true_probability=0.65,
        resolution_risk_score=0.55,
        expected_value_per_contract=2.0,
        rationale="same shape",
        cohort_wallets_involved=["0xaf4"],
        timestamp=datetime.now(UTC),
    )
    res = evaluate_gates(
        belief=belief, cohort_side="YES",
        spread_cost_per_contract=0.04,
        depth_ok=True, concentration_ok=True,
        mode="degen",
    )
    assert res.overall_passed is True


# ── Arcada shape (bid-price MTM) ────────────────────────


def test_arcada_bid_price_mtm_differs_from_mid() -> None:
    """Arcada §5.3.1: mid-price MTM systematically overstates by the
    bid-ask spread. A position bought at 50c with bid=48/ask=52 marks
    to 48c per share under our valuation, NOT 50c (mid)."""
    val = value_position(
        position_id=1,
        size_shares=200,
        avg_entry_price_cents=50,
        bid_price_cents=48,
        ask_price_cents=52,
    )
    assert val.value_cents == 200 * 48
    # Mid-price hypothetical (we never report this number; just verify
    # the gap matches the spread).
    mid_value_hypothetical = 200 * 50
    overstatement = mid_value_hypothetical - val.value_cents
    assert overstatement == 200 * 2                  # exactly the half-spread
    # Spread itself is 4c, half-spread = 2c x 200 shares = 400c gap.
    assert overstatement == 400


# ── Arbitrage null ──────────────────────────────────────


def test_arb_detection_skips_yes_plus_no_below_one_dollar() -> None:
    """arXiv:2508.03474 shape: YES + NO sums below $1 → arb opportunity
    we deliberately skip (paper sim cannot model ms-latency races)."""
    arb = check_arb_pattern(yes_ask_cents=42, no_ask_cents=54)
    # 42 + 54 = 96; deviation 4c > 2c threshold → arb detected.
    assert arb.arb_detected is True
    assert arb.deviation_cents == 4
    # Healthy book: 50c + 52c = 102c → not arb.
    healthy = check_arb_pattern(yes_ask_cents=50, no_ask_cents=52)
    assert healthy.arb_detected is False


# ── Decision-gate directional veto (H6 mechanism) ──────


def test_decision_gate_directional_veto_persists_in_systematic_and_degen() -> None:
    """Cohort says YES, investigator estimates P(YES) < 0.5 → VETO,
    even with high confidence + EV. Non-negotiable per §5.4."""
    belief = Belief(
        market_id="m_x",
        category="event_analysis",
        confidence=0.95,
        estimated_true_probability=0.30,
        resolution_risk_score=0.05,
        expected_value_per_contract=15.0,
        rationale="cohort is wrong",
        cohort_wallets_involved=["0xaf4"],
        timestamp=datetime.now(UTC),
    )
    for mode in ("systematic", "degen"):
        res = evaluate_gates(
            belief=belief, cohort_side="YES",
            spread_cost_per_contract=0.04,
            depth_ok=True, concentration_ok=True,
            mode=mode,  # type: ignore[arg-type]
        )
        assert res.overall_passed is False
        d = next(o for o in res.outcomes if o.name == "d_directional")
        assert d.passed is False


# ── Niche weighting consensus ──────────────────────────


def test_niche_weighting_consensus_math() -> None:
    """5-wallet synthetic: signal_strength is the unweighted sum of
    edge_likelihoods after the 2026-04-28 calibration that flattened
    NICHE_WEIGHT to 1.0 (resolved-market analysis showed niche pools
    were diluting, not adding edge — see scripts/cohort_pnl_on_resolved.py).

        signal_strength = sum(weight_i * edge_likelihood_i)

    With current weights (all 1.0):
      0.80 + 0.70 + 0.65 + 0.55 + 0.50 = 3.20
    """
    wallets = [
        CohortWalletInMarket("0xa", "aec", 0.80, "YES"),
        CohortWalletInMarket("0xb", "ai_labs", 0.70, "YES"),
        CohortWalletInMarket("0xc", "general", 0.65, "YES"),
        CohortWalletInMarket("0xd", "general", 0.55, "YES"),
        CohortWalletInMarket("0xe", "general", 0.50, "YES"),
    ]
    res = aggregate_signal(wallets)
    expected = (
        NICHE_WEIGHT * 0.80 + NICHE_WEIGHT * 0.70
        + GENERAL_WEIGHT * 0.65
        + GENERAL_WEIGHT * 0.55
        + GENERAL_WEIGHT * 0.50
    )
    assert res.signal_strength == pytest.approx(expected, rel=1e-9)
    assert res.signal_strength == pytest.approx(3.20, rel=1e-9)
    assert res.n_niche == 2
    assert res.n_general == 3
    assert res.dominant_side == "YES"          # all on the same side
    assert res.above_threshold is True


def test_consensus_split_returns_no_dominant_side() -> None:
    """If the cohort is roughly split between YES and NO, return NONE
    so the executor doesn't act on weak/contradictory signal."""
    wallets = [
        CohortWalletInMarket("0xa", "aec", 0.80, "YES"),
        CohortWalletInMarket("0xb", "ai_labs", 0.75, "NO"),
        CohortWalletInMarket("0xc", "general", 0.60, "YES"),
        CohortWalletInMarket("0xd", "general", 0.60, "NO"),
    ]
    res = aggregate_signal(wallets)
    assert res.dominant_side == "NONE"
