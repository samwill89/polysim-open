"""Paired t-test tests — build plan §5.5."""

from __future__ import annotations

from polysim.evaluator.significance import paired_t_test


def test_primary_beats_baseline_clearly() -> None:
    # Primary has ~+1% daily consistently; baseline ~0.
    primary = [0.01, 0.011, 0.009, 0.012, 0.008, 0.01, 0.011, 0.009, 0.012, 0.01]
    baseline = [0.0, 0.001, -0.001, 0.0, 0.0, -0.001, 0.001, 0.0, -0.001, 0.001]
    result = paired_t_test(primary, baseline)
    assert result.passes is True
    assert result.p_value < 0.05
    assert result.t_statistic > 0
    assert result.n_samples == len(primary)


def test_identical_series_fails() -> None:
    r = [0.01, -0.01, 0.01, -0.01] * 3
    result = paired_t_test(r, r)
    # mean_diff == 0 -> passes=False even though p_value could be 1.
    assert not result.passes


def test_baseline_beats_primary_fails() -> None:
    primary = [0.0] * 10
    baseline = [0.01, 0.011, 0.009, 0.012, 0.008, 0.01, 0.011, 0.009, 0.012, 0.01]
    result = paired_t_test(primary, baseline)
    assert not result.passes


def test_too_few_samples_fails() -> None:
    result = paired_t_test([0.01], [0.0])
    assert result.passes is False
    assert result.p_value == 1.0


def test_length_mismatch_truncates() -> None:
    primary = [0.01] * 5
    baseline = [0.0] * 20
    result = paired_t_test(primary, baseline)
    assert result.n_samples == 5
