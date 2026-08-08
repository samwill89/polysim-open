"""Hypothesis-test sanity tests — empirical-priors §8.1.

We don't assert *correct* p-values for any specific scenario (that's
scipy's job). We assert:
  - tests return TestResult shapes
  - small-n cases return ambiguous, never crash
  - obviously-positive scenarios produce accept_alt
  - obviously-null scenarios produce reject_alt or ambiguous (never accept)
"""

from __future__ import annotations

import random

from polysim.experiment.hypotheses import (
    h1_systematic_returns_positive,
    h2_degen_higher_mean,
    h3_niche_outperforms_general,
    h4_edge_likelihood_predicts_pnl,
    h5_lunar_top_pnl_predicts_forward,
    h6_investigator_judgment_adds_value,
)


def test_h1_small_n_is_ambiguous() -> None:
    res = h1_systematic_returns_positive([0.01, 0.02])
    assert res.hypothesis_id == "H1"
    assert res.verdict == "ambiguous"


def test_h1_clearly_positive_returns_accepts() -> None:
    rng = random.Random(0)
    series = [rng.gauss(0.005, 0.005) for _ in range(60)]
    res = h1_systematic_returns_positive(series, alpha=0.05)
    assert res.verdict == "accept_alt"
    assert res.p_value < 0.05


def test_h1_zero_mean_does_not_falsely_accept() -> None:
    rng = random.Random(1)
    series = [rng.gauss(0.0, 0.01) for _ in range(60)]
    res = h1_systematic_returns_positive(series, alpha=0.05)
    assert res.verdict in {"reject_alt", "ambiguous"}


def test_h2_with_higher_degen_mean_accepts() -> None:
    rng = random.Random(2)
    sys_r = [rng.gauss(0.001, 0.005) for _ in range(60)]
    deg_r = [rng.gauss(0.010, 0.020) for _ in range(60)]
    res = h2_degen_higher_mean(sys_r, deg_r, alpha=0.10)
    assert res.verdict == "accept_alt"


def test_h3_niche_better_when_actually_better() -> None:
    rng = random.Random(3)
    niche = [rng.gauss(50, 30) for _ in range(40)]
    gen = [rng.gauss(0, 30) for _ in range(40)]
    res = h3_niche_outperforms_general(niche, gen, alpha=0.10)
    assert res.verdict == "accept_alt"


def test_h4_high_correlation_accepts() -> None:
    rng = random.Random(4)
    el = [rng.uniform(0, 1) for _ in range(60)]
    pnl = [e * 100 + rng.gauss(0, 20) for e in el]
    res = h4_edge_likelihood_predicts_pnl(el, pnl, alpha=0.05)
    assert res.verdict == "accept_alt"
    assert res.statistic > 0.3


def test_h4_no_correlation_rejects() -> None:
    rng = random.Random(5)
    el = [rng.uniform(0, 1) for _ in range(60)]
    pnl = [rng.gauss(0, 20) for _ in range(60)]
    res = h4_edge_likelihood_predicts_pnl(el, pnl, alpha=0.05)
    assert res.verdict in {"reject_alt", "ambiguous"}


def test_h5_zero_correlation() -> None:
    rng = random.Random(6)
    hist = [rng.gauss(0, 100) for _ in range(60)]
    fwd = [rng.gauss(0, 100) for _ in range(60)]
    res = h5_lunar_top_pnl_predicts_forward(hist, fwd, alpha=0.05)
    assert res.verdict in {"reject_alt", "ambiguous"}


def test_h6_judgment_layer_adds_value_when_actually_does() -> None:
    rng = random.Random(7)
    passed = [rng.gauss(50, 20) for _ in range(40)]
    vetoed = [rng.gauss(-30, 20) for _ in range(40)]
    res = h6_investigator_judgment_adds_value(passed, vetoed, alpha=0.05)
    assert res.verdict == "accept_alt"


def test_h6_no_difference_does_not_accept() -> None:
    rng = random.Random(8)
    passed = [rng.gauss(0, 20) for _ in range(40)]
    vetoed = [rng.gauss(0, 20) for _ in range(40)]
    res = h6_investigator_judgment_adds_value(passed, vetoed, alpha=0.05)
    assert res.verdict in {"reject_alt", "ambiguous"}
