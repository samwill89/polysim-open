"""Equity sentiment track — a parallel paper-trading lane for US AI / semis /
memory / robotics stocks, driven by social attention + stance signals.

Separate from the Polymarket copy-trade track: its own tables (equity_*),
its own daily loop, its own tournament of variants. Reuses paper_runs
(tag='equity_v1') for the variant accounts.
"""

from __future__ import annotations

from polysim.equity.universe import EQUITY_UNIVERSE, ETF_BENCHMARKS

__all__ = ["EQUITY_UNIVERSE", "ETF_BENCHMARKS"]
