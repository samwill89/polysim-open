"""Tier-1 classifier — empirical-priors addendum §3.2 / §5.1.

Pure features. No LLM. Produces `edge_likelihood` per wallet, both
globally and per-niche. Near-zero cost per wallet — runs over thousands
of wallets in a single nightly tick.

Design:
  edge_likelihood = sigmoid( w_wr  · z(win_rate)
                           + w_tc  · z(log trade_count)
                           + w_pnl · z(pnl_lifetime_cents)
                           - w_hd  · z(early_exit_ratio)
                           - w_cc  · z(counterparty_concentration) )

Weights are constants (versioned with the prompt-style schema). The
classifier is deliberately *non-trainable* in the first version so we
can get a clean H4 reading: does this hand-crafted score predict
forward 30-day P&L? If yes, we have a baseline; if no, we know the
features themselves don't carry edge and we don't waste cycles tuning.

Stable under feature-order permutation (property tested in §9.3).
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

from polysim.discovery.features import WalletFeatures

CLASSIFIER_VERSION: Final[str] = "1.0.0"

# Default weights (positive features add to edge; risk features subtract).
DEFAULT_WEIGHTS: Final[dict[str, float]] = {
    "win_rate": 1.6,
    "log_trade_count": 0.7,
    "pnl_lifetime": 1.2,
    "early_exit_ratio": 0.5,            # subtracted
    "counterparty_concentration": 0.4,   # subtracted
}


@dataclass(frozen=True)
class ClassifierScore:
    wallet_address: str
    scope: str
    edge_likelihood: float               # 0..1
    components: dict[str, float]         # contribution per feature, for audit


def _sigmoid(x: float) -> float:
    if x > 50:
        return 1.0
    if x < -50:
        return 0.0
    return 1.0 / (1.0 + math.exp(-x))


def _zscore(values: list[float]) -> tuple[float, float]:
    """Sample mean + stdev, with a tiny epsilon to avoid div-by-zero."""
    if not values:
        return 0.0, 1.0
    mean = sum(values) / len(values)
    var = sum((v - mean) ** 2 for v in values) / max(1, len(values))
    sd = math.sqrt(var) or 1e-9
    return mean, sd


def _z(value: float, mean: float, sd: float) -> float:
    return (value - mean) / sd


def classify_population(
    features: Iterable[WalletFeatures],
    *,
    weights: dict[str, float] | None = None,
) -> list[ClassifierScore]:
    """Score every (wallet, scope) row.

    Per-scope normalization: AEC features get z-scored against AEC peers,
    AI labs against AI-labs peers. This avoids a small-niche wallet
    looking weak just because the global mean is dominated by the long
    tail of casual wallets.

    The output is ordered by (scope, edge_likelihood DESC) so callers
    can take top-N per scope without re-sorting.
    """
    weights = weights or DEFAULT_WEIGHTS
    rows = list(features)
    if not rows:
        return []

    # Group by scope for per-scope normalization.
    by_scope: dict[str, list[WalletFeatures]] = {}
    for f in rows:
        by_scope.setdefault(f.scope, []).append(f)

    scored: list[ClassifierScore] = []
    for scope, scope_rows in by_scope.items():
        wr_mean, wr_sd = _zscore([f.win_rate for f in scope_rows])
        tc_mean, tc_sd = _zscore([math.log(max(1, f.trade_count)) for f in scope_rows])
        pnl_mean, pnl_sd = _zscore([float(f.pnl_lifetime_cents) for f in scope_rows])
        ee_mean, ee_sd = _zscore([f.early_exit_ratio for f in scope_rows])
        cc_mean, cc_sd = _zscore([f.counterparty_concentration for f in scope_rows])
        for f in scope_rows:
            comp = {
                "win_rate": weights["win_rate"] * _z(f.win_rate, wr_mean, wr_sd),
                "log_trade_count": weights["log_trade_count"]
                    * _z(math.log(max(1, f.trade_count)), tc_mean, tc_sd),
                "pnl_lifetime": weights["pnl_lifetime"]
                    * _z(float(f.pnl_lifetime_cents), pnl_mean, pnl_sd),
                "early_exit_ratio": -weights["early_exit_ratio"]
                    * _z(f.early_exit_ratio, ee_mean, ee_sd),
                "counterparty_concentration": -weights["counterparty_concentration"]
                    * _z(f.counterparty_concentration, cc_mean, cc_sd),
            }
            score = _sigmoid(sum(comp.values()))
            scored.append(ClassifierScore(
                wallet_address=f.wallet_address,
                scope=scope,
                edge_likelihood=score,
                components=comp,
            ))
    scored.sort(key=lambda s: (s.scope, -s.edge_likelihood))
    return scored


__all__ = [
    "CLASSIFIER_VERSION",
    "DEFAULT_WEIGHTS",
    "ClassifierScore",
    "classify_population",
]
