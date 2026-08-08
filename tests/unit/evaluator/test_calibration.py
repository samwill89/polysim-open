"""Calibration tests — build plan §5.6."""

from __future__ import annotations

from polysim.evaluator.calibration import (
    ascii_plot,
    brier_score,
    is_monotonic,
    summary,
)


def _bucket(range_: str, n: int, hit: float) -> dict[str, object]:
    return {"range": range_, "n": n, "hit_rate": hit}


def test_ascii_plot_empty() -> None:
    out = ascii_plot([])
    assert "no closed positions" in out


def test_ascii_plot_renders_bars() -> None:
    buckets = [_bucket("5.0-5.5", 10, 0.4), _bucket("8.0-8.5", 5, 0.8)]
    out = ascii_plot(buckets)
    # Must include both ranges and both bar lengths.
    assert "5.0-5.5" in out
    assert "8.0-8.5" in out
    assert "#" in out


def test_is_monotonic_true_when_weakly_increasing() -> None:
    buckets = [
        _bucket("5.0-5.5", 10, 0.5),
        _bucket("5.5-6.0", 8, 0.6),
        _bucket("6.0-6.5", 5, 0.6),
    ]
    assert is_monotonic(buckets)


def test_is_monotonic_false_on_dip() -> None:
    buckets = [
        _bucket("5.0-5.5", 10, 0.5),
        _bucket("5.5-6.0", 8, 0.4),
    ]
    assert not is_monotonic(buckets)


def test_brier_score_on_perfect_calibration() -> None:
    # Bucket centre = (lo + 0.25)/10 = 0.525 vs hit_rate=0.525 → zero Brier.
    buckets = [_bucket("5.0-5.5", 100, 0.525)]
    assert brier_score(buckets) < 1e-9


def test_brier_score_on_mismatch() -> None:
    buckets = [_bucket("5.0-5.5", 100, 0.0)]  # predicted 0.525, got 0
    score = brier_score(buckets)
    assert 0.25 < score < 0.3   # (0.525)^2


def test_summary_shape() -> None:
    buckets = [_bucket("5.0-5.5", 10, 0.5)]
    out = summary(buckets)
    assert out["monotonic"] is True
    assert "ascii" in out
    assert "buckets" in out
