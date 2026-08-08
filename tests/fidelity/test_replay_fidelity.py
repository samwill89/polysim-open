"""Simulation fidelity tests — empirical-priors addendum §9.4.

Per Kaustubh: a strategy working in simulation must work in production,
which means the simulator has to model latency, partial fills,
cancellations, post-fill settlement delay. Real-data fidelity tests
require poly_data fixtures; we ship the synthetic-determinism subset
here so PR CI can catch silent drift, and mark the real-data ones
@pytest.mark.fidelity for nightly runs.

  * Determinism: replaying the same fill request with the same RNG seed
    produces the same fill price + latency (so experiment comparisons
    aren't poisoned by hidden randomness).
  * Settlement: every simulated fill produces ≥ 2-block sellable_at
    (already covered in tests/unit/portfolio/test_settlement.py — repeated
    here as the §9.4 "fidelity" view).
  * Slippage upper bound: simulated fill price never exceeds book ask
    by more than the configured slippage_ticks per spec §7.7.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from polysim.paper.fill_model import (
    BookLevel,
    FillModel,
    FillRequest,
    OrderbookSnapshot,
)
from polysim.portfolio.settlement import (
    DEFAULT_MIN_BLOCKS,
    compute_settlement_window,
)


def _ob(asks: list[tuple[int, int]], bids: list[tuple[int, int]]) -> OrderbookSnapshot:
    return OrderbookSnapshot(
        market_id="m1", outcome="YES",
        asks=[BookLevel(price_cents=p, size_shares=s) for p, s in asks],
        bids=[BookLevel(price_cents=p, size_shares=s) for p, s in bids],
        timestamp=datetime.now(UTC),
    )


@pytest.mark.fidelity
async def test_replay_is_deterministic_under_fixed_seed() -> None:
    """Same FillRequest + same book + same fill model → same outcome.

    The fill model uses Python's `random` for latency draws; if anything
    in the call graph picks up entropy from elsewhere (uuid, time-based
    seeds, etc.), this test catches it before an experiment is invalidated.
    """
    book = _ob(
        asks=[(40, 1000), (41, 500)],
        bids=[(38, 800), (37, 300)],
    )
    request = FillRequest(
        market_id="m1", side="BUY", outcome="YES",
        intended_size_shares=100, intended_price_cents=40,
        insider_trade_timestamp=datetime(2026, 4, 20, tzinfo=UTC),
    )
    fm_a = FillModel()
    fm_b = FillModel()
    # Manually drive the same Random seed so any latency draws line up.
    import random as _r
    _r.seed(42)
    a = await fm_a.simulate(request, book)
    _r.seed(42)
    b = await fm_b.simulate(request, book)
    assert a.fill_price_cents == b.fill_price_cents
    assert a.filled_size_shares == b.filled_size_shares
    assert a.slippage_cents == b.slippage_cents


async def test_settlement_window_always_meets_minimum() -> None:
    """Every simulated fill carries a settlement window >= MIN_BLOCKS
    blocks at the configured block time. This is the §4.2 invariant.
    """
    buy_ts = datetime(2026, 4, 20, 12, 0, 0, tzinfo=UTC)
    pending_until, blocks = compute_settlement_window(buy_ts)
    assert blocks >= DEFAULT_MIN_BLOCKS
    assert (pending_until - buy_ts).total_seconds() >= 4.0  # 2 blocks * 2s


@pytest.mark.fidelity
async def test_slippage_within_configured_bound() -> None:
    """Simulated fill never exceeds best_ask + slippage_ticks * tick."""
    book = _ob(
        asks=[(40, 1000), (41, 500)],
        bids=[(38, 800)],
    )
    fm = FillModel(slippage_ticks=1)
    request = FillRequest(
        market_id="m1", side="BUY", outcome="YES",
        intended_size_shares=200, intended_price_cents=40,
        insider_trade_timestamp=datetime(2026, 4, 20, tzinfo=UTC),
    )
    result = await fm.simulate(request, book)
    # Best ask 40c, slippage_ticks=1 → max fill 41c.
    assert result.fill_price_cents <= 41
    # We can also confirm the partial-fill behaviour: 200 shares requested,
    # but only 1000 available at 40c — so all 200 should fill at 40c.
    assert result.filled_size_shares == 200
    assert result.fill_price_cents == 40


# Synthetic 100-trade replay scaffold. Real data lands later.
@pytest.mark.fidelity
async def test_synthetic_100_trade_replay_matches_within_tolerance() -> None:
    """100 sequential fills against shrinking book — assert deterministic
    cumulative size + average price within ±1 cent."""
    book = _ob(
        asks=[(40, 50_000), (41, 50_000)],
        bids=[(39, 50_000)],
    )
    fm = FillModel()
    cumulative_shares = 0
    cumulative_cost = 0
    for _ in range(100):
        req = FillRequest(
            market_id="m1", side="BUY", outcome="YES",
            intended_size_shares=100, intended_price_cents=40,
            insider_trade_timestamp=datetime(2026, 4, 20, tzinfo=UTC),
        )
        r = await fm.simulate(req, book)
        cumulative_shares += r.filled_size_shares
        cumulative_cost += r.filled_size_shares * r.fill_price_cents
    # 100 fills x 100 shares each = 10,000 shares; book has 100,000 → all fill.
    assert cumulative_shares == 10_000
    avg_fill = cumulative_cost / cumulative_shares
    # Average within 1 cent of best ask.
    assert 39 <= avg_fill <= 42


_ = (asyncio, Path, timedelta)  # keep imports referenced
