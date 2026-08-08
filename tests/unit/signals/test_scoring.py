"""Composite / multiplier / gate behavior — the money-touching math."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from polysim.signals.schema import MarketSignal
from polysim.signals.scoring import (
    compose_conviction,
    conviction_multiplier,
    signal_confidence,
    signal_gate_blocks,
)

TS = datetime(2026, 7, 1, tzinfo=UTC)


def _sig(composite: float, confidence: float) -> MarketSignal:
    return MarketSignal(
        market_id="m1", ts=TS, composite=composite, confidence=confidence,
    )


# ── composite ────────────────────────────────────────────


def test_composite_neutral_inputs_near_half() -> None:
    c, _ = compose_conviction(
        attention_z=0.0, velocity=1.0, engagement=0.5, breadth=0.5,
    )
    assert 0.45 <= c <= 0.55


def test_composite_spike_scores_high() -> None:
    c, comps = compose_conviction(
        attention_z=3.0, velocity=4.0, engagement=0.8, breadth=0.9,
    )
    assert c > 0.8
    assert comps["term_attention"] > 0.9


def test_composite_dead_conversation_scores_low() -> None:
    c, _ = compose_conviction(
        attention_z=-3.0, velocity=0.2, engagement=0.0, breadth=0.0,
    )
    assert c < 0.2


def test_composite_always_in_unit_interval() -> None:
    for z in (-50.0, 0.0, 50.0):
        for v in (0.0, 1.0, 100.0):
            c, _ = compose_conviction(
                attention_z=z, velocity=v, engagement=1.0, breadth=1.0,
            )
            assert 0.0 <= c <= 1.0


# ── confidence ───────────────────────────────────────────


def test_confidence_full_when_covered() -> None:
    assert signal_confidence(n_matched_posts=5, baseline_points=10) == 1.0


def test_confidence_scales_with_coverage() -> None:
    half = signal_confidence(
        n_matched_posts=3, baseline_points=2,
        min_matched_posts=3, min_baseline_points=4,
    )
    assert half == pytest.approx(0.5)


def test_confidence_capped_on_community_fallback() -> None:
    c = signal_confidence(
        n_matched_posts=50, baseline_points=50, community_fallback=True,
    )
    assert c == 0.5


def test_confidence_zero_without_posts() -> None:
    assert signal_confidence(n_matched_posts=0, baseline_points=50) == 0.0


# ── conviction multiplier ────────────────────────────────


def test_multiplier_missing_signal_is_exactly_one() -> None:
    assert conviction_multiplier(None) == 1.0


def test_multiplier_bounds() -> None:
    hi = conviction_multiplier(_sig(1.0, 1.0), min_mult=0.5, max_mult=1.5)
    lo = conviction_multiplier(_sig(0.0, 1.0), min_mult=0.5, max_mult=1.5)
    assert hi == pytest.approx(1.5)
    assert lo == pytest.approx(0.5)


def test_multiplier_neutral_at_midpoint() -> None:
    assert conviction_multiplier(_sig(0.5, 1.0)) == pytest.approx(1.0)


def test_multiplier_low_confidence_pulls_toward_one() -> None:
    strong = conviction_multiplier(_sig(1.0, 1.0))
    weak = conviction_multiplier(_sig(1.0, 0.2))
    assert strong == pytest.approx(1.5)
    assert weak == pytest.approx(1.1)


def test_multiplier_zero_confidence_is_one() -> None:
    assert conviction_multiplier(_sig(1.0, 0.0)) == 1.0
    assert conviction_multiplier(_sig(0.0, 0.0)) == 1.0


# ── gate ─────────────────────────────────────────────────


def test_gate_fail_open_on_missing_signal() -> None:
    assert signal_gate_blocks(None) is False


def test_gate_fail_open_on_low_confidence() -> None:
    assert signal_gate_blocks(_sig(0.01, 0.1)) is False


def test_gate_blocks_confident_dead_conversation() -> None:
    assert signal_gate_blocks(_sig(0.05, 0.9)) is True


def test_gate_open_for_live_conversation() -> None:
    assert signal_gate_blocks(_sig(0.6, 0.9)) is False
