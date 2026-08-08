"""Strategy tournament — Phase Q.

Runs N variants of the cohort-copy strategy concurrently as separate
paper runs, periodically scores them, and reallocates capital toward
winners by pausing/resuming runs.

This is NOT auto-tuning hyperparameters in flight (which would overfit
to noise given our signal density). It's a multi-armed-bandit over a
pre-registered pool of variants with weekly-cadence rebalancing.
"""

from polysim.tournament.allocator import RunScore, TournamentAllocator
from polysim.tournament.loop import TournamentAllocatorLoop
from polysim.tournament.variants import VARIANTS, Variant

__all__ = [
    "VARIANTS",
    "RunScore",
    "TournamentAllocator",
    "TournamentAllocatorLoop",
    "Variant",
]
