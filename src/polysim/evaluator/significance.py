"""Paired t-test on daily returns — spec gap G15 / build plan §5.5.

Spec §10 credibility gate: primary must beat each baseline at p < 0.05.
`paired_t_test` returns the SciPy result plus a boolean verdict.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class TTestResult:
    t_statistic: float
    p_value: float
    n_samples: int
    alpha: float
    passes: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "t_statistic": self.t_statistic,
            "p_value": self.p_value,
            "n_samples": self.n_samples,
            "alpha": self.alpha,
            "passes": self.passes,
        }


def paired_t_test(
    primary: Sequence[float],
    baseline: Sequence[float],
    *,
    alpha: float = 0.05,
) -> TTestResult:
    """Paired t-test, `primary - baseline` mean > 0 hypothesis.

    Pairs are aligned element-wise; if lengths differ the longer series is
    truncated to the shorter length (equal-length assumption of ttest_rel).
    Returns `passes=True` iff p_value < alpha AND the mean difference is
    positive (primary outperformed).
    """
    n = min(len(primary), len(baseline))
    if n < 2:
        return TTestResult(
            t_statistic=0.0,
            p_value=1.0,
            n_samples=n,
            alpha=alpha,
            passes=False,
        )
    p = list(primary[:n])
    b = list(baseline[:n])

    # Lazy import so unit tests that don't need scipy don't pay for it.
    from scipy.stats import ttest_rel

    result = ttest_rel(p, b)
    t = float(result.statistic)
    pv = float(result.pvalue)

    mean_diff = sum(pi - bi for pi, bi in zip(p, b, strict=False)) / n
    passes = (pv < alpha) and (mean_diff > 0.0) and math.isfinite(pv)

    return TTestResult(
        t_statistic=t if math.isfinite(t) else 0.0,
        p_value=pv if math.isfinite(pv) else 1.0,
        n_samples=n,
        alpha=alpha,
        passes=passes,
    )
