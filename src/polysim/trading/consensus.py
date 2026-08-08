"""Consensus weighting — empirical-priors addendum §3.5 + §5.4.

Computes `signal_strength` as the weighted sum of cohort-wallet
edge_likelihood scores observed in a market. Niche-matched wallets get
1.5x weight; general-pool wallets get 1.0x.

  signal_strength = sum_i (wallet_weight_i * edge_likelihood_i)

Used by the executor to decide whether a market is worth invoking the
investigator on (gated by `min_investigate_threshold`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

NICHE_WEIGHT: Final[float] = 1.0  # Was 1.5; calibrated 2026-04-28 — niche pools were diluting, not helping (resolved-market analysis: general +42% ROI vs niches flat/negative).
GENERAL_WEIGHT: Final[float] = 1.0
DEFAULT_MIN_INVESTIGATE_THRESHOLD: Final[float] = 1.5


@dataclass(frozen=True)
class CohortWalletInMarket:
    """One cohort wallet's position in a candidate market."""

    address: str
    pool: str                            # 'aec' | 'ai_labs' | 'creator_econ' | 'general'
    edge_likelihood: float               # 0..1
    side: str                            # 'YES' | 'NO'


@dataclass(frozen=True)
class ConsensusResult:
    """Weighted aggregate of cohort signal in a market."""

    signal_strength: float
    yes_strength: float
    no_strength: float
    dominant_side: str                   # 'YES' | 'NO' | 'NONE'
    n_wallets: int
    n_niche: int
    n_general: int
    above_threshold: bool


def weight_for(pool: str) -> float:
    """Niche pools (aec/ai_labs/creator_econ) get 1.5x; general gets 1.0x."""
    return NICHE_WEIGHT if pool != "general" else GENERAL_WEIGHT


def aggregate_signal(
    wallets: list[CohortWalletInMarket],
    *,
    min_investigate_threshold: float = DEFAULT_MIN_INVESTIGATE_THRESHOLD,
) -> ConsensusResult:
    """Combine cohort wallet positions in one market into a signal.

    Per §3.5: niche-matched wallets carry 1.5x weight. The dominant_side
    is whichever side accumulates more weighted edge — decisive consensus
    only when |yes_strength - no_strength| >= 0.25 of the total weight
    (else "NONE", meaning cohort is split with no clean signal).
    """
    yes = 0.0
    no = 0.0
    n_niche = 0
    n_general = 0
    for w in wallets:
        weight = weight_for(w.pool)
        contribution = weight * float(w.edge_likelihood)
        if w.side.upper() == "YES":
            yes += contribution
        elif w.side.upper() == "NO":
            no += contribution
        if w.pool == "general":
            n_general += 1
        else:
            n_niche += 1
    total = yes + no
    dominant = (
        "NONE" if abs(yes - no) < 0.25 * max(total, 1e-9)
        else "YES" if yes > no else "NO"
    )
    return ConsensusResult(
        signal_strength=total,
        yes_strength=yes,
        no_strength=no,
        dominant_side=dominant,
        n_wallets=len(wallets),
        n_niche=n_niche,
        n_general=n_general,
        above_threshold=total >= min_investigate_threshold,
    )


__all__ = [
    "DEFAULT_MIN_INVESTIGATE_THRESHOLD",
    "GENERAL_WEIGHT",
    "NICHE_WEIGHT",
    "CohortWalletInMarket",
    "ConsensusResult",
    "aggregate_signal",
    "weight_for",
]
