"""Arbitrage-pattern detector — empirical-priors addendum §11 Q1 + §9.2.

Polymarket arbitrage (YES + NO < $1.00 across correlated markets) was
documented in arXiv:2508.03474 — $39.7M extracted in 12 months by
quant traders with ms-latency infrastructure. PolySim explicitly avoids
this play (§2 air gap is incompatible with sub-second arbitrage), so we
*detect* arb-pattern markets and SKIP rather than attempt to trade them.

The detector is pure: given a list of price quotes for a binary market,
return whether the implied total deviates from $1.00 enough to indicate
arb is in progress.

This module's only job is to mark `arb_detected, skip` so the gate's
rejection log shows "we deliberately did not trade this; not a missed
opportunity".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

# Threshold: how far the YES+NO sum can deviate from $1.00 before we
# declare an arb in progress and skip. 2¢ deviation accommodates normal
# spread + slippage; >2¢ is outside expected book noise.
DEFAULT_ARB_THRESHOLD_CENTS: Final[int] = 2


@dataclass(frozen=True)
class ArbCheck:
    """Outcome of an arb-pattern check on one binary market."""

    yes_ask_cents: int
    no_ask_cents: int
    sum_cents: int
    deviation_cents: int                  # |sum - 100|
    arb_detected: bool                    # true → skip per addendum §11 Q1
    reason: str = ""


def check_arb_pattern(
    *,
    yes_ask_cents: int,
    no_ask_cents: int,
    threshold_cents: int = DEFAULT_ARB_THRESHOLD_CENTS,
) -> ArbCheck:
    """A binary market with YES_ask + NO_ask materially below 100c is
    a guaranteed-profit setup that ms-latency arb bots will sweep before
    we can fill at paper-equivalent prices. Skip it."""
    total = int(yes_ask_cents) + int(no_ask_cents)
    deviation = abs(total - 100)
    arb = total < (100 - threshold_cents)
    return ArbCheck(
        yes_ask_cents=int(yes_ask_cents),
        no_ask_cents=int(no_ask_cents),
        sum_cents=total,
        deviation_cents=deviation,
        arb_detected=arb,
        reason=(
            f"YES+NO asks = {total}c (threshold {100 - threshold_cents}c) "
            "— arb opportunity, paper sim should skip"
            if arb else ""
        ),
    )


__all__ = [
    "DEFAULT_ARB_THRESHOLD_CENTS",
    "ArbCheck",
    "check_arb_pattern",
]
