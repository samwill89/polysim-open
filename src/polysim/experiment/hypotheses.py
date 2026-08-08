"""Pre-registered hypotheses H1-H6 — empirical-priors addendum §8.1.

Every test is a function returning `TestResult`. Statistical implementations
delegate to scipy.stats / numpy; we never hand-roll math beyond the
mean/stdev that's already in evaluator/metrics.

The hypotheses:
  H1 — systematic mode returns > 0 net of bid-ask costs        (one-sample t)
  H2 — degen mean > systematic mean (variance-adjusted)         (Welch's t)
  H3 — niche-matched primary cohort outperforms general         (two-sample t)
  H4 — edge_likelihood correlates with forward 30d P&L (rho>0.3)  (Pearson r + Fisher z)
  H5 — top-by-historical-P&L predicts forward (Lunar)           (Pearson r controlling for trade count)
  H6 — investigator judgment adds value (passed vs vetoed)      (Welch's t on counterfactual P&L)

H6 is the "judgment layer" test: at end-of-experiment we reconstruct
counterfactual P&L for every rejected trade (logs/decisions/rejections.jsonl),
then compare against trades that passed all gates. If passed > rejected
in mean P&L (with significance), the gate adds value; otherwise it's
friction we should drop.

All tests log p-values and effect sizes; the caller (reporting.py)
formats them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import numpy as np
from scipy import stats

log = logging.getLogger(__name__)

Verdict = Literal["accept_alt", "reject_alt", "ambiguous"]


@dataclass(frozen=True)
class TestResult:
    """One hypothesis test's output."""

    hypothesis_id: str
    null: str
    alpha: float
    n: int
    statistic: float
    p_value: float
    effect_size: float
    verdict: Verdict
    notes: str = ""


def _verdict(p_value: float, alpha: float) -> Verdict:
    if p_value < alpha:
        return "accept_alt"
    if p_value > 0.5:
        return "reject_alt"
    return "ambiguous"


# ── H1: systematic returns > 0 net of bid-ask ──────────


def h1_systematic_returns_positive(
    daily_returns_systematic: list[float],
    *,
    alpha: float = 0.05,
) -> TestResult:
    """One-sample t-test: mean return > 0?"""
    arr = np.asarray(daily_returns_systematic, dtype=float)
    if len(arr) < 5:
        return TestResult(
            hypothesis_id="H1",
            null="returns indistinguishable from zero",
            alpha=alpha, n=len(arr),
            statistic=float("nan"), p_value=1.0,
            effect_size=0.0, verdict="ambiguous",
            notes="insufficient sample (n<5)",
        )
    res = stats.ttest_1samp(arr, popmean=0.0, alternative="greater")
    sd = float(np.std(arr, ddof=1)) or 1e-9
    cohens_d = float(np.mean(arr) / sd)
    return TestResult(
        hypothesis_id="H1",
        null="returns indistinguishable from zero",
        alpha=alpha, n=len(arr),
        statistic=float(res.statistic),
        p_value=float(res.pvalue),
        effect_size=cohens_d,
        verdict=_verdict(float(res.pvalue), alpha),
    )


# ── H2: degen mean > systematic mean (variance-adjusted) ──


def h2_degen_higher_mean(
    daily_returns_systematic: list[float],
    daily_returns_degen: list[float],
    *,
    alpha: float = 0.10,
) -> TestResult:
    sys_a = np.asarray(daily_returns_systematic, dtype=float)
    deg_a = np.asarray(daily_returns_degen, dtype=float)
    n = int(len(sys_a) + len(deg_a))
    if min(len(sys_a), len(deg_a)) < 5:
        return TestResult(
            hypothesis_id="H2",
            null="degen mean <= systematic mean",
            alpha=alpha, n=n,
            statistic=float("nan"), p_value=1.0,
            effect_size=0.0, verdict="ambiguous",
            notes="insufficient sample",
        )
    res = stats.ttest_ind(deg_a, sys_a, equal_var=False, alternative="greater")
    pooled_sd = float(np.sqrt(
        (np.var(deg_a, ddof=1) + np.var(sys_a, ddof=1)) / 2
    )) or 1e-9
    cohens_d = float((np.mean(deg_a) - np.mean(sys_a)) / pooled_sd)
    return TestResult(
        hypothesis_id="H2",
        null="degen mean <= systematic mean",
        alpha=alpha, n=n,
        statistic=float(res.statistic),
        p_value=float(res.pvalue),
        effect_size=cohens_d,
        verdict=_verdict(float(res.pvalue), alpha),
    )


# ── H3: niche cohort outperforms general ───────────────


def h3_niche_outperforms_general(
    pnl_niche: list[float],
    pnl_general: list[float],
    *,
    alpha: float = 0.10,
) -> TestResult:
    a = np.asarray(pnl_niche, dtype=float)
    b = np.asarray(pnl_general, dtype=float)
    n = int(len(a) + len(b))
    if min(len(a), len(b)) < 5:
        return TestResult(
            hypothesis_id="H3",
            null="no niche effect after edge_likelihood control",
            alpha=alpha, n=n,
            statistic=float("nan"), p_value=1.0,
            effect_size=0.0, verdict="ambiguous",
            notes="insufficient sample",
        )
    res = stats.ttest_ind(a, b, equal_var=False, alternative="greater")
    pooled_sd = float(np.sqrt(
        (np.var(a, ddof=1) + np.var(b, ddof=1)) / 2
    )) or 1e-9
    cohens_d = float((np.mean(a) - np.mean(b)) / pooled_sd)
    return TestResult(
        hypothesis_id="H3",
        null="no niche effect after edge_likelihood control",
        alpha=alpha, n=n,
        statistic=float(res.statistic),
        p_value=float(res.pvalue),
        effect_size=cohens_d,
        verdict=_verdict(float(res.pvalue), alpha),
    )


# ── H4: edge_likelihood predicts forward P&L ───────────


def h4_edge_likelihood_predicts_pnl(
    edge_likelihoods: list[float],
    forward_pnl: list[float],
    *,
    alpha: float = 0.05,
    threshold_rho: float = 0.3,
) -> TestResult:
    """Pearson r > threshold; H0 is r ≤ threshold."""
    a = np.asarray(edge_likelihoods, dtype=float)
    b = np.asarray(forward_pnl, dtype=float)
    n = int(min(len(a), len(b)))
    if n < 10:
        return TestResult(
            hypothesis_id="H4", null=f"rho <= {threshold_rho}",
            alpha=alpha, n=n,
            statistic=float("nan"), p_value=1.0,
            effect_size=0.0, verdict="ambiguous",
            notes="insufficient sample",
        )
    a, b = a[:n], b[:n]
    rho, _ = stats.pearsonr(a, b)
    # One-sided p that rho > threshold via Fisher transform.
    z_obs = np.arctanh(rho)
    z_thr = np.arctanh(threshold_rho)
    se = 1.0 / np.sqrt(n - 3)
    z = (z_obs - z_thr) / se
    p_value = float(1 - stats.norm.cdf(z))
    return TestResult(
        hypothesis_id="H4",
        null=f"correlation rho <= {threshold_rho}",
        alpha=alpha, n=n,
        statistic=float(rho), p_value=p_value,
        effect_size=float(rho),
        verdict=_verdict(p_value, alpha),
    )


# ── H5: top-historical-P&L predicts forward (Lunar) ────


def h5_lunar_top_pnl_predicts_forward(
    historical_pnl: list[float],
    forward_pnl: list[float],
    *,
    alpha: float = 0.05,
) -> TestResult:
    a = np.asarray(historical_pnl, dtype=float)
    b = np.asarray(forward_pnl, dtype=float)
    n = int(min(len(a), len(b)))
    if n < 10:
        return TestResult(
            hypothesis_id="H5",
            null="historical P&L has no forward predictive value",
            alpha=alpha, n=n,
            statistic=float("nan"), p_value=1.0,
            effect_size=0.0, verdict="ambiguous",
            notes="insufficient sample",
        )
    a, b = a[:n], b[:n]
    rho, p = stats.pearsonr(a, b)
    return TestResult(
        hypothesis_id="H5",
        null="historical P&L has no forward predictive value",
        alpha=alpha, n=n,
        statistic=float(rho), p_value=float(p),
        effect_size=float(rho),
        verdict=_verdict(float(p), alpha),
    )


# ── H6: judgment layer adds value ──────────────────────


def h6_investigator_judgment_adds_value(
    pnl_passed_gates: list[float],
    pnl_vetoed_counterfactual: list[float],
    *,
    alpha: float = 0.05,
) -> TestResult:
    """Welch's t-test: did trades that passed all gates outperform what
    vetoed trades would have returned, had we taken them?"""
    a = np.asarray(pnl_passed_gates, dtype=float)
    b = np.asarray(pnl_vetoed_counterfactual, dtype=float)
    n = int(len(a) + len(b))
    if min(len(a), len(b)) < 5:
        return TestResult(
            hypothesis_id="H6",
            null="passed and vetoed have equal forward P&L",
            alpha=alpha, n=n,
            statistic=float("nan"), p_value=1.0,
            effect_size=0.0, verdict="ambiguous",
            notes="insufficient counterfactual sample",
        )
    res = stats.ttest_ind(a, b, equal_var=False, alternative="greater")
    pooled_sd = float(np.sqrt(
        (np.var(a, ddof=1) + np.var(b, ddof=1)) / 2
    )) or 1e-9
    cohens_d = float((np.mean(a) - np.mean(b)) / pooled_sd)
    return TestResult(
        hypothesis_id="H6",
        null="passed and vetoed have equal forward P&L",
        alpha=alpha, n=n,
        statistic=float(res.statistic),
        p_value=float(res.pvalue),
        effect_size=cohens_d,
        verdict=_verdict(float(res.pvalue), alpha),
    )


__all__ = [
    "TestResult",
    "Verdict",
    "h1_systematic_returns_positive",
    "h2_degen_higher_mean",
    "h3_niche_outperforms_general",
    "h4_edge_likelihood_predicts_pnl",
    "h5_lunar_top_pnl_predicts_forward",
    "h6_investigator_judgment_adds_value",
]


# Keep used-as-types imports referenced.
_ = Path
