"""Fill-model unit tests — build plan §4.11.

Edge cases:
  * empty book -> 0 fill, best_price defaulted to 0
  * depth < size -> partial fill under ABANDON
  * huge depth -> full fill at best price
  * slippage_ticks respected
  * CHASE walks up to 3 more ticks
  * 2x pessimism when no live snapshot (G8)
  * zero / extreme latency config
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from polysim.paper.fill_model import (
    BookLevel,
    FillModel,
    FillRequest,
    OrderbookSnapshot,
    _round_half_up,
    _sample_lognormal,
    _walk_book,
)


def _fill_request(
    *,
    side: str = "BUY",
    outcome: str = "YES",
    size: int = 1000,
    price: int = 40,
    market: str = "m1",
) -> FillRequest:
    return FillRequest(
        market_id=market,
        side=side,  # type: ignore[arg-type]
        outcome=outcome,  # type: ignore[arg-type]
        intended_size_shares=size,
        intended_price_cents=price,
        insider_trade_timestamp=datetime(2026, 4, 19, 14, tzinfo=UTC),
    )


def _book(
    asks: list[tuple[int, int]] | None = None,
    bids: list[tuple[int, int]] | None = None,
) -> OrderbookSnapshot:
    return OrderbookSnapshot(
        market_id="m1",
        outcome="YES",
        asks=[BookLevel(price_cents=p, size_shares=s) for p, s in (asks or [])],
        bids=[BookLevel(price_cents=p, size_shares=s) for p, s in (bids or [])],
        timestamp=datetime(2026, 4, 19, 14, tzinfo=UTC),
    )


class TestWalkBook:
    def test_walks_all_at_best_when_cheapest_has_depth(self) -> None:
        levels = [BookLevel(price_cents=40, size_shares=1000)]
        filled, notional, consumed = _walk_book(
            levels=levels, side="BUY", target_shares=500, limit_price=40
        )
        assert filled == 500
        assert notional == 500 * 40
        assert len(consumed) == 1

    def test_walks_multiple_levels(self) -> None:
        levels = [
            BookLevel(price_cents=40, size_shares=100),
            BookLevel(price_cents=41, size_shares=200),
            BookLevel(price_cents=42, size_shares=500),
        ]
        filled, notional, _ = _walk_book(
            levels=levels, side="BUY", target_shares=500, limit_price=42
        )
        # 100*40 + 200*41 + 200*42 = 4000 + 8200 + 8400 = 20600
        assert filled == 500
        assert notional == 20_600

    def test_respects_limit_price(self) -> None:
        levels = [
            BookLevel(price_cents=40, size_shares=100),
            BookLevel(price_cents=50, size_shares=900),
        ]
        filled, _, _ = _walk_book(
            levels=levels, side="BUY", target_shares=500, limit_price=45
        )
        # Only the 40-cent level is acceptable -> 100 shares filled.
        assert filled == 100

    def test_empty_levels(self) -> None:
        filled, notional, consumed = _walk_book(
            levels=[], side="BUY", target_shares=100, limit_price=50
        )
        assert filled == 0 and notional == 0 and consumed == []

    def test_sell_walks_bids_descending(self) -> None:
        levels = [
            BookLevel(price_cents=60, size_shares=50),
            BookLevel(price_cents=55, size_shares=100),
        ]
        filled, notional, _ = _walk_book(
            levels=levels, side="SELL", target_shares=100, limit_price=55
        )
        assert filled == 100
        assert notional == 50 * 60 + 50 * 55


class TestSampleLognormal:
    def test_degenerate_p50_returned(self) -> None:
        import random
        rng = random.Random(0)
        # p95 <= p50 triggers the short-circuit path.
        assert _sample_lognormal(rng, p50_ms=100, p95_ms=50) == 100.0

    def test_zero_p50_returns_zero(self) -> None:
        import random
        rng = random.Random(0)
        assert _sample_lognormal(rng, p50_ms=0, p95_ms=10) == 0.0

    def test_samples_positive(self) -> None:
        import random
        rng = random.Random(0)
        vals = [_sample_lognormal(rng, p50_ms=1000, p95_ms=3000) for _ in range(100)]
        assert all(v > 0 for v in vals)
        # Rough median bracket (log-normal is skewed; allow a wide range).
        median = sorted(vals)[len(vals) // 2]
        assert 500 <= median <= 2000


class TestRoundHalfUp:
    @pytest.mark.parametrize(("x", "expected"), [
        (0.4, 0), (0.5, 1), (0.6, 1),
        (1.5, 2), (2.5, 3),      # round-half-up, not banker's
        (-0.5, 0),               # -0.5 + 0.5 = 0 -> floor(0) = 0
    ])
    def test_round(self, x: float, expected: int) -> None:
        assert _round_half_up(x) == expected


@pytest.mark.asyncio
class TestSimulate:
    async def test_empty_book_returns_zero(self) -> None:
        fm = FillModel(rng_seed=0)
        result = await fm.simulate(_fill_request(), _book())
        assert result.filled_size_shares == 0
        assert result.fee_cents == 0

    async def test_deep_book_full_fill_at_best(self) -> None:
        fm = FillModel(rng_seed=0, slippage_ticks=5)
        book = _book(asks=[(40, 100_000)])
        result = await fm.simulate(_fill_request(size=1000, price=40), book)
        assert result.filled_size_shares == 1000
        assert result.fill_price_cents == 40
        assert result.slippage_cents == 0

    async def test_partial_fill_abandon(self) -> None:
        fm = FillModel(rng_seed=0, slippage_ticks=1, on_partial="ABANDON")
        book = _book(asks=[(40, 100), (41, 100), (45, 100_000)])
        # Want 1000; ABANDON + slippage_ticks=1 means stop after 41.
        result = await fm.simulate(_fill_request(size=1000, price=40), book)
        assert result.filled_size_shares == 200  # 100 @ 40 + 100 @ 41

    async def test_partial_fill_chase(self) -> None:
        fm = FillModel(rng_seed=0, slippage_ticks=1, on_partial="CHASE",
                       chase_max_extra_ticks=3)
        book = _book(asks=[(40, 100), (41, 100), (42, 100), (43, 100), (44, 10_000)])
        result = await fm.simulate(_fill_request(size=1000, price=40), book)
        # CHASE extends to best+slippage+3 = 44. All five levels acceptable.
        assert result.filled_size_shares == 1000

    async def test_slippage_cost(self) -> None:
        fm = FillModel(rng_seed=0, slippage_ticks=5)
        # Intended 40, but book starts at 42.
        book = _book(asks=[(42, 10_000)])
        result = await fm.simulate(_fill_request(size=100, price=40), book)
        assert result.fill_price_cents == 42
        assert result.slippage_cents == 2  # 42 - 40

    async def test_sell_uses_bids(self) -> None:
        fm = FillModel(rng_seed=0, slippage_ticks=5)
        book = _book(bids=[(60, 100)])
        result = await fm.simulate(
            _fill_request(side="SELL", size=100, price=58), book
        )
        assert result.filled_size_shares == 100
        assert result.fill_price_cents == 60

    async def test_fee_computed_in_bps(self) -> None:
        fm = FillModel(rng_seed=0, fee_bps=100)  # 1% fee
        book = _book(asks=[(40, 1_000)])
        result = await fm.simulate(_fill_request(size=1000, price=40), book)
        # Notional = 1000*40 = 40,000 cents; fee = 1% = 400
        assert result.fee_cents == 400

    async def test_synthetic_book_used_when_no_snapshot(self) -> None:
        fm = FillModel(rng_seed=0, slippage_ticks=1)
        result = await fm.simulate(_fill_request(size=100, price=40), None)
        assert result.used_synthetic_book is True
        assert result.filled_size_shares > 0

    async def test_synthetic_applies_2x_pessimism(self) -> None:
        """With no snapshot, slippage budget is 2x the configured ticks.

        Synthesised 5-level book at 40, 41, 42, 43, 44. slippage_ticks=1.
        Live budget = 1 -> can walk 40, 41 only.
        Synthetic budget = 2 -> can walk 40, 41, 42.
        """
        fm = FillModel(rng_seed=0, slippage_ticks=1, on_partial="ABANDON",
                       historical_pessimism_multiplier=2.0)
        result = await fm.simulate(_fill_request(size=10_000, price=40), None)
        consumed_prices = {lvl.price_cents for lvl in result.levels_consumed}
        assert 42 in consumed_prices, "2x pessimism should let us walk to 42"

    async def test_extreme_latency_nonnegative(self) -> None:
        fm = FillModel(
            rng_seed=0,
            detection_latency_p50_ms=100_000,
            detection_latency_p95_ms=500_000,
            decision_latency_p50_ms=50_000,
            decision_latency_p95_ms=300_000,
        )
        book = _book(asks=[(40, 100)])
        result = await fm.simulate(_fill_request(size=100, price=40), book)
        assert result.latency_ms >= 0

    async def test_zero_latency_config(self) -> None:
        fm = FillModel(
            rng_seed=0,
            detection_latency_p50_ms=0, detection_latency_p95_ms=0,
            decision_latency_p50_ms=0, decision_latency_p95_ms=0,
        )
        book = _book(asks=[(40, 100)])
        result = await fm.simulate(_fill_request(size=100, price=40), book)
        assert result.latency_ms == 0

    async def test_invalid_slippage_ticks_rejected(self) -> None:
        with pytest.raises(ValueError):
            FillModel(slippage_ticks=-1)
