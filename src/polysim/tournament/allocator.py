"""Tournament allocator — scoring + decisions.

Pure functions over (run_id, starting_balance, current_balance,
realized_pnl, unrealized_pnl, n_resolved_positions, max_drawdown_pct). The
TournamentAllocatorLoop wires this to the DB.

Scoring is conservative on purpose: a run only gets retired if its
underperformance is BOTH significantly below the cohort median AND
has enough resolved outcome history to not be a fluke. The default minimum
trial size is 30 resolved positions before any retirement decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Verdict = Literal["promote", "hold", "retire"]


@dataclass(frozen=True)
class RunScore:
    """Per-run snapshot used by the allocator."""

    run_id: int
    variant_name: str
    starting_balance_cents: int
    current_balance_cents: int
    realized_pnl_cents: int
    unrealized_pnl_cents: int
    n_positions: int
    n_resolved_positions: int
    max_drawdown_pct: float
    paused: bool

    @property
    def total_pnl_cents(self) -> int:
        return self.realized_pnl_cents + self.unrealized_pnl_cents

    @property
    def return_pct(self) -> float:
        if self.starting_balance_cents <= 0:
            return 0.0
        return self.total_pnl_cents / self.starting_balance_cents


@dataclass(frozen=True)
class AllocatorDecision:
    """One row in the allocator's per-rebalance output."""

    run_id: int
    variant_name: str
    verdict: Verdict
    reason: str
    return_pct: float
    rank: int


class TournamentAllocator:
    """Decides which runs to pause / resume / promote on each rebalance.

    Strategy:
      * Rank active runs by total return (realized + unrealized).
      * Retire any run with enough resolved trials (>= min_trial_positions),
        return below `retire_threshold_pct`, AND ranked in the bottom
        `retire_fraction` of the field.
      * Promote any run ranked in the top `promote_fraction` AND
        positive return — promotion = unpause if paused, otherwise no-op
        (we don't currently dynamically rebalance capital between runs).
      * Hold the rest.

    A paused run with return >= 0 stays paused (we don't resume losers
    by default — only the allocator's promote step can revive them).
    """

    def __init__(
        self,
        *,
        min_trial_positions: int = 30,
        retire_threshold_pct: float = -0.15,    # -15%
        retire_fraction: float = 0.20,          # bottom 20%
        promote_fraction: float = 0.20,         # top 20%
    ) -> None:
        if not (0.0 < retire_fraction < 1.0):
            raise ValueError("retire_fraction must be in (0, 1)")
        if not (0.0 < promote_fraction < 1.0):
            raise ValueError("promote_fraction must be in (0, 1)")
        self.min_trial_positions = min_trial_positions
        self.retire_threshold_pct = retire_threshold_pct
        self.retire_fraction = retire_fraction
        self.promote_fraction = promote_fraction

    def rebalance(self, scores: list[RunScore]) -> list[AllocatorDecision]:
        if not scores:
            return []
        # Rank by return — best first.
        ranked = sorted(scores, key=lambda s: -s.return_pct)
        n = len(ranked)
        retire_count = max(1, int(n * self.retire_fraction))
        promote_count = max(1, int(n * self.promote_fraction))
        retire_ids = {r.run_id for r in ranked[-retire_count:]}
        promote_ids = {r.run_id for r in ranked[:promote_count]}

        decisions: list[AllocatorDecision] = []
        for rank, s in enumerate(ranked, start=1):
            verdict: Verdict
            reason: str
            if (
                s.run_id in retire_ids
                and s.n_resolved_positions >= self.min_trial_positions
                and s.return_pct <= self.retire_threshold_pct
                and not s.paused
            ):
                verdict = "retire"
                reason = (
                    f"return {s.return_pct*100:.1f}% <= "
                    f"{self.retire_threshold_pct*100:.0f}% threshold "
                    f"after {s.n_resolved_positions} resolved positions"
                )
            elif (
                s.run_id in promote_ids
                and s.return_pct > 0
                and s.n_resolved_positions >= self.min_trial_positions
                and s.paused
            ):
                verdict = "promote"
                reason = (
                    f"top-{self.promote_fraction*100:.0f}% performer; "
                    f"return {s.return_pct*100:+.1f}%"
                )
            else:
                verdict = "hold"
                if s.n_resolved_positions < self.min_trial_positions:
                    reason = (
                        "insufficient resolved trial: "
                        f"{s.n_resolved_positions}/"
                        f"{self.min_trial_positions} outcomes"
                    )
                elif s.paused:
                    reason = "paused, not in top tier"
                else:
                    reason = f"rank {rank}/{n}, holding"
            decisions.append(AllocatorDecision(
                run_id=s.run_id,
                variant_name=s.variant_name,
                verdict=verdict,
                reason=reason,
                return_pct=s.return_pct,
                rank=rank,
            ))
        return decisions


__all__ = ["AllocatorDecision", "RunScore", "TournamentAllocator", "Verdict"]
