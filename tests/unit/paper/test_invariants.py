"""Property-based portfolio + fill-model invariants — build plan §4.12."""

from __future__ import annotations

from datetime import UTC, datetime

from hypothesis import given, settings
from hypothesis import strategies as st

from polysim.paper.fill_model import (
    BookLevel,
    FillModel,
    FillRequest,
    OrderbookSnapshot,
    _walk_book,
)

_LEVEL = st.builds(
    BookLevel,
    price_cents=st.integers(min_value=1, max_value=99),
    size_shares=st.integers(min_value=1, max_value=10_000),
)


@given(
    levels_raw=st.lists(_LEVEL, min_size=1, max_size=10),
    target=st.integers(min_value=1, max_value=100_000),
    limit=st.integers(min_value=1, max_value=100),
)
@settings(max_examples=100, deadline=None)
def test_walk_book_never_fills_more_than_requested(
    levels_raw: list[BookLevel], target: int, limit: int
) -> None:
    # Sort levels ascending; walk-the-book expects monotone on BUY side.
    levels = sorted(levels_raw, key=lambda lvl: lvl.price_cents)
    filled, _notional, consumed = _walk_book(
        levels=levels, side="BUY", target_shares=target, limit_price=limit
    )
    assert filled <= target
    # All levels consumed were within the limit.
    for lvl in consumed:
        assert lvl.price_cents <= limit


@given(
    levels_raw=st.lists(_LEVEL, min_size=1, max_size=10),
    target=st.integers(min_value=1, max_value=100_000),
)
@settings(max_examples=100, deadline=None)
def test_walk_book_notional_equals_sum_of_consumed(
    levels_raw: list[BookLevel], target: int
) -> None:
    levels = sorted(levels_raw, key=lambda lvl: lvl.price_cents)
    filled, notional, consumed = _walk_book(
        levels=levels, side="BUY", target_shares=target, limit_price=100,
    )
    expected = sum(lvl.price_cents * lvl.size_shares for lvl in consumed)
    assert notional == expected
    assert sum(lvl.size_shares for lvl in consumed) == filled


@given(
    size=st.integers(min_value=1, max_value=100_000),
    price=st.integers(min_value=1, max_value=99),
    depth=st.integers(min_value=10_000, max_value=1_000_000),
    seed=st.integers(min_value=0, max_value=1_000),
)
@settings(max_examples=40, deadline=None)
async def test_simulate_slippage_nonnegative(
    size: int, price: int, depth: int, seed: int
) -> None:
    """Slippage is defined as max(0, fill - intended). Must never be negative."""
    fm = FillModel(rng_seed=seed, slippage_ticks=5)
    book = OrderbookSnapshot(
        market_id="m",
        outcome="YES",
        asks=[BookLevel(price_cents=price, size_shares=depth)],
        bids=[BookLevel(price_cents=max(1, price - 1), size_shares=depth)],
        timestamp=datetime(2026, 4, 19, tzinfo=UTC),
    )
    req = FillRequest(
        market_id="m", side="BUY", outcome="YES",
        intended_size_shares=size, intended_price_cents=price,
        insider_trade_timestamp=datetime(2026, 4, 19, tzinfo=UTC),
    )
    result = await fm.simulate(req, book)
    assert result.slippage_cents >= 0
    assert result.fee_cents >= 0
    assert result.filled_size_shares <= size
