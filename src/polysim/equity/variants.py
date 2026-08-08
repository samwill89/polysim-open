"""Equity strategy registry with explicit live selection.

Each variant decides, given the daily context (signals + price indicators),
which tickers it wants to hold. Historical sentiment variants stay registered
for reproducibility; ``DEFAULT_LIVE_VARIANTS`` is the compact forward-paper
set selected from the latest walk-forward review.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class EquityVariant:
    name: str
    description: str
    kind: str                      # see EquityExecutor for handling
    # entry gates
    min_composite: float = 0.8
    min_z_attn: float = 1.5
    require_uptrend: bool = True   # close>SMA50 and SMA20>SMA50
    max_5d_runup: float = 0.15     # skip blow-off tops (reversal guard)
    # selection
    top_n: int = 8
    rank_by: str = "composite"     # composite | momentum | z_attn
    # exits (ignored by buy_and_hold)
    buy_and_hold: bool = False
    trail_sma: int = 20            # exit when close < SMA(trail_sma)
    atr_mult: float = 3.0
    hard_stop_pct: float = 0.08
    time_stop_days: int = 10
    # sizing
    max_positions: int = 8
    per_position_cap_pct: float = 0.15
    sector_cap_pct: float = 0.40
    contrarian: bool = False       # invert attention preference
    symbols: tuple[str, ...] | None = None


EQUITY_VARIANTS: list[EquityVariant] = [
    EquityVariant(
        name="ew_bh",
        description="Equal-weight buy-and-hold the whole universe (control).",
        kind="buy_and_hold", buy_and_hold=True, max_positions=99,
        per_position_cap_pct=1.0, sector_cap_pct=1.0, require_uptrend=False,
    ),
    EquityVariant(
        name="momentum",
        description="Top-8 by 3-mo momentum above SMA50, no sentiment (control).",
        kind="momentum", rank_by="momentum", require_uptrend=True,
        min_composite=-99, min_z_attn=-99,
    ),
    EquityVariant(
        name="sentiment_only",
        description="Buy on composite>=0.8 + attention spike; no trend gate.",
        kind="sentiment", require_uptrend=False,
    ),
    EquityVariant(
        name="sent_momentum",
        description="Flagship: bullish sentiment AND price uptrend; hold winners.",
        kind="sentiment", require_uptrend=True,
    ),
    EquityVariant(
        name="attention_only",
        description="Enter on attention spike (z>=2), direction from trend.",
        kind="attention", min_z_attn=2.0, min_composite=-99,
        require_uptrend=True,
    ),
    EquityVariant(
        name="contrarian",
        description="Fade the crowd: out-of-favor names in quiet uptrends.",
        kind="contrarian", contrarian=True, min_composite=-99,
        min_z_attn=-99, require_uptrend=True, max_5d_runup=0.08,
    ),
    EquityVariant(
        name="smh_bh",
        description="Buy-and-hold SMH as the investable semiconductor benchmark.",
        kind="buy_and_hold", buy_and_hold=True, max_positions=1,
        per_position_cap_pct=1.0, sector_cap_pct=1.0,
        require_uptrend=False, symbols=("SMH",),
    ),
    EquityVariant(
        name="momentum_slow",
        description=(
            "Top-8 3-month momentum with SMA50/ATR4 exits and a 63-day cap."
        ),
        kind="momentum", rank_by="momentum", require_uptrend=True,
        min_composite=-99, min_z_attn=-99, max_5d_runup=1.0,
        trail_sma=50, atr_mult=4.0, hard_stop_pct=0.12,
        time_stop_days=63,
    ),
]

DEFAULT_LIVE_VARIANTS: tuple[str, ...] = ("smh_bh", "momentum_slow")


def variants_by_name() -> dict[str, EquityVariant]:
    return {v.name: v for v in EQUITY_VARIANTS}


def select_variants(names: Sequence[str] | None = None) -> list[EquityVariant]:
    """Return variants by name, preserving caller order.

    ``None`` means the full research set. Live startup passes
    DEFAULT_LIVE_VARIANTS so underperforming sentiment/attention lanes stay
    parked unless explicitly requested.
    """
    if names is None:
        return list(EQUITY_VARIANTS)

    by_name = variants_by_name()
    selected: list[EquityVariant] = []
    unknown: list[str] = []
    for raw in names:
        name = str(raw).strip()
        if not name:
            continue
        variant = by_name.get(name)
        if variant is None:
            unknown.append(name)
            continue
        selected.append(variant)
    if unknown:
        raise ValueError(f"unknown equity variant(s): {', '.join(unknown)}")
    return selected
