"""Composite scorer tests — pure-function compose() + should_flag()."""

from __future__ import annotations

import math

from polysim.models import DetectorSignal
from polysim.scoring.composite import CompositeScorer


def _signal(
    name: str, raw: float, conf: float = 1.0, trade_id: str = "t1"
) -> DetectorSignal:
    # Use model_construct when raw is non-finite — pydantic's ge/le on raw_score
    # otherwise rejects NaN/Inf, which is what we're trying to test flows through
    # the scorer's defensive quarantine path.
    if not math.isfinite(raw) or not math.isfinite(conf):
        return DetectorSignal.model_construct(
            wallet_address="0xaf4",
            market_id="m1",
            trade_id=trade_id,
            detector_name=name,
            raw_score=raw,
            confidence=conf,
            components={},
            evidence={},
        )
    return DetectorSignal(
        wallet_address="0xaf4",
        market_id="m1",
        trade_id=trade_id,
        detector_name=name,
        raw_score=raw,
        confidence=conf,
        components={},
        evidence={},
    )


def _scorer() -> CompositeScorer:
    return CompositeScorer(
        weights={
            "CategoryInsiderDetector": 3.5,
            "EventInsiderDetector": 2.5,
            "TimingDetector": 1.5,
            "CoordinationDetector": 1.5,
            "FreshWalletDetector": 1.0,
        },
        flag_threshold=5.0,
        min_contributing_detectors=2,
    )


def test_weighted_sum_clamped_to_ten() -> None:
    sc = _scorer()
    # Every detector at raw=1.0, conf=1.0 → weighted sum = 10.0 exactly.
    signals = [
        _signal("CategoryInsiderDetector", 1.0),
        _signal("EventInsiderDetector", 1.0),
        _signal("TimingDetector", 1.0),
        _signal("CoordinationDetector", 1.0),
        _signal("FreshWalletDetector", 1.0),
    ]
    c = sc.compose(wallet_address="0xaf4", market_id="m1", signals=signals)
    assert c.score == 10.0
    assert set(c.contributing_detectors) == {s.detector_name for s in signals}


def test_threshold_gate_requires_two_detectors() -> None:
    sc = _scorer()
    # Only Category at 1.0 → weighted = 3.5 (below 5.0 threshold)
    c = sc.compose(
        wallet_address="0xaf4",
        market_id="m1",
        signals=[_signal("CategoryInsiderDetector", 1.0)],
    )
    assert c.score < 5.0
    assert not sc.should_flag(c)

    # Category full + Event half → 3.5 + 1.25 = 4.75 (still below 5.0)
    c2 = sc.compose(
        wallet_address="0xaf4",
        market_id="m1",
        signals=[
            _signal("CategoryInsiderDetector", 1.0),
            _signal("EventInsiderDetector", 0.5),
        ],
    )
    assert not sc.should_flag(c2)

    # Category full + Event full → 3.5 + 2.5 = 6.0 and 2 detectors → flags.
    c3 = sc.compose(
        wallet_address="0xaf4",
        market_id="m1",
        signals=[
            _signal("CategoryInsiderDetector", 1.0),
            _signal("EventInsiderDetector", 1.0),
        ],
    )
    assert sc.should_flag(c3)


def test_fresh_wallet_alone_never_flags() -> None:
    """G11 — FreshWalletDetector must not solo-flag."""
    sc = _scorer()
    # Even at max raw+conf, weight 1.0 * 1.0 * 1.0 = 1.0 < 5.0 AND only one detector.
    c = sc.compose(
        wallet_address="0xaf4",
        market_id="m1",
        signals=[_signal("FreshWalletDetector", 1.0)],
    )
    assert not sc.should_flag(c)


def test_nan_signal_is_quarantined() -> None:
    """§14 #7 — NaN must not contaminate the composite."""
    sc = _scorer()
    c = sc.compose(
        wallet_address="0xaf4",
        market_id="m1",
        signals=[
            _signal("CategoryInsiderDetector", 1.0),
            _signal("EventInsiderDetector", float("nan")),
            _signal("TimingDetector", 1.0),
        ],
    )
    # NaN signal dropped; only Category + Timing contribute = 3.5 + 1.5 = 5.0
    assert math.isfinite(c.score)
    assert c.score == 5.0


def test_none_signals_ignored() -> None:
    sc = _scorer()
    c = sc.compose(
        wallet_address="0xaf4",
        market_id="m1",
        signals=[
            None,
            _signal("CategoryInsiderDetector", 1.0),
            None,
            _signal("EventInsiderDetector", 1.0),
        ],
    )
    assert c.score == 6.0


def test_reproducibility() -> None:
    """§14 #5 — identical inputs produce identical outputs (modulo created_at)."""
    sc1 = _scorer()
    sc2 = _scorer()
    signals = [
        _signal("CategoryInsiderDetector", 0.8, conf=0.9),
        _signal("EventInsiderDetector", 0.7, conf=0.8),
    ]
    c1 = sc1.compose(wallet_address="0xaf4", market_id="m1", signals=signals)
    c2 = sc2.compose(wallet_address="0xaf4", market_id="m1", signals=signals)
    assert c1.score == c2.score
    assert c1.components == c2.components
    assert c1.contributing_detectors == c2.contributing_detectors


def test_to_flag_populates_per_detector_breakdown() -> None:
    sc = _scorer()
    signals = [
        _signal("CategoryInsiderDetector", 0.9),
        _signal("EventInsiderDetector", 0.8),
    ]
    c = sc.compose(wallet_address="0xaf4", market_id="m1", signals=signals)
    flag = sc.to_flag(c, signals, trade_id="t1")
    assert flag.detector_name == "composite"
    assert flag.composite_score == c.score
    assert "per_detector" in flag.components
    per_det = flag.components["per_detector"]
    assert isinstance(per_det, dict)
    assert "CategoryInsiderDetector" in per_det
    assert "EventInsiderDetector" in per_det
