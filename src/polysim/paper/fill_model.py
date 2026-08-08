"""Fill model — spec §9. The single biggest honesty risk in the project.

Pessimism is mandatory. The fill model simulates:
  * Detection + decision latency, sampled from a log-normal whose median
    matches the configured p50 and whose 95th percentile matches p95.
  * Walking the book (ascending asks for BUY, descending bids for SELL).
  * Slippage tolerance capped at `slippage_ticks` cents above/below the
    best price; the ABANDON policy stops at that cap; the CHASE policy
    walks up to 3 more cents before giving up.
  * Fees computed in basis points of notional.
  * G8 — when no live orderbook snapshot is provided, synthesize one
    anchored on the insider's fill price and widen slippage by 2x.
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from polysim.models import Outcome, Side


@dataclass(frozen=True)
class BookLevel:
    price_cents: int  # 0..100
    size_shares: int


@dataclass(frozen=True)
class OrderbookSnapshot:
    market_id: str
    outcome: Outcome
    asks: Sequence[BookLevel]  # ascending by price (best ask first)
    bids: Sequence[BookLevel]  # descending by price (best bid first)
    timestamp: datetime


@dataclass(frozen=True)
class FillRequest:
    market_id: str
    side: Side
    outcome: Outcome
    intended_size_shares: int
    intended_price_cents: int          # insider's price (the "target")
    insider_trade_timestamp: datetime


@dataclass(frozen=True)
class FillResult:
    filled_size_shares: int
    fill_price_cents: int              # volume-weighted average
    slippage_cents: int                # fill - intended (>=0 for buys that paid up)
    latency_ms: int
    fee_cents: int
    used_synthetic_book: bool = False
    # Per-level contributions, useful for debugging + unit tests
    levels_consumed: tuple[BookLevel, ...] = field(default_factory=tuple)


class FillModel:
    def __init__(
        self,
        *,
        detection_latency_p50_ms: int = 3_000,
        detection_latency_p95_ms: int = 10_000,
        decision_latency_p50_ms: int = 2_000,
        decision_latency_p95_ms: int = 15_000,
        slippage_ticks: int = 1,
        on_partial: Literal["ABANDON", "CHASE"] = "ABANDON",
        fee_bps: int = 0,
        historical_pessimism_multiplier: float = 2.0,
        chase_max_extra_ticks: int = 3,
        rng_seed: int | None = None,
    ) -> None:
        if slippage_ticks < 0:
            raise ValueError("slippage_ticks must be >= 0")
        self.detection_latency_p50_ms = detection_latency_p50_ms
        self.detection_latency_p95_ms = detection_latency_p95_ms
        self.decision_latency_p50_ms = decision_latency_p50_ms
        self.decision_latency_p95_ms = decision_latency_p95_ms
        self.slippage_ticks = slippage_ticks
        self.on_partial = on_partial
        self.fee_bps = fee_bps
        self.historical_pessimism_multiplier = historical_pessimism_multiplier
        self.chase_max_extra_ticks = chase_max_extra_ticks
        self._rng = random.Random(rng_seed) if rng_seed is not None else random.Random()

    async def simulate(
        self,
        request: FillRequest,
        book: OrderbookSnapshot | None,
    ) -> FillResult:
        latency_ms = self._sample_total_latency_ms()
        used_synthetic = book is None
        effective_book = book if book is not None else self._synthesize_book(request)
        slippage_budget = self._slippage_budget(used_synthetic)

        levels = (
            effective_book.asks if request.side == "BUY" else effective_book.bids
        )
        if not levels:
            return FillResult(
                filled_size_shares=0,
                fill_price_cents=0,
                slippage_cents=0,
                latency_ms=latency_ms,
                fee_cents=0,
                used_synthetic_book=used_synthetic,
                levels_consumed=tuple(),
            )

        best_price = levels[0].price_cents
        # Acceptable worst price under the configured tolerance.
        limit = (
            best_price + slippage_budget
            if request.side == "BUY"
            else best_price - slippage_budget
        )
        # CHASE extends the cap up to `chase_max_extra_ticks`.
        if self.on_partial == "CHASE":
            if request.side == "BUY":
                limit += self.chase_max_extra_ticks
            else:
                limit -= self.chase_max_extra_ticks

        filled, notional_cents, consumed = _walk_book(
            levels=levels,
            side=request.side,
            target_shares=request.intended_size_shares,
            limit_price=limit,
        )
        if filled == 0:
            return FillResult(
                filled_size_shares=0,
                fill_price_cents=best_price,
                slippage_cents=0,
                latency_ms=latency_ms,
                fee_cents=0,
                used_synthetic_book=used_synthetic,
                levels_consumed=tuple(),
            )

        vwap = _round_half_up(notional_cents / filled)
        slippage = (
            vwap - request.intended_price_cents
            if request.side == "BUY"
            else request.intended_price_cents - vwap
        )
        slippage = max(0, slippage)
        fee = _round_half_up((filled * vwap * self.fee_bps) / 10_000)

        return FillResult(
            filled_size_shares=filled,
            fill_price_cents=vwap,
            slippage_cents=slippage,
            latency_ms=latency_ms,
            fee_cents=fee,
            used_synthetic_book=used_synthetic,
            levels_consumed=tuple(consumed),
        )

    # ── helpers ──────────────────────────────────────────

    def _sample_total_latency_ms(self) -> int:
        det = _sample_lognormal(
            self._rng,
            p50_ms=self.detection_latency_p50_ms,
            p95_ms=self.detection_latency_p95_ms,
        )
        dec = _sample_lognormal(
            self._rng,
            p50_ms=self.decision_latency_p50_ms,
            p95_ms=self.decision_latency_p95_ms,
        )
        return max(0, round(det + dec))

    def _slippage_budget(self, used_synthetic: bool) -> int:
        if used_synthetic:
            # G8 — double the slippage when we don't have a real snapshot.
            return max(
                0,
                round(self.slippage_ticks * self.historical_pessimism_multiplier),
            )
        return self.slippage_ticks

    def _synthesize_book(self, request: FillRequest) -> OrderbookSnapshot:
        """Anchor a synthetic book around the insider's price.

        Assumes thin depth (we're trading niche markets); enforces a
        non-trivial 5-level ladder so partial-fill paths still exercise.
        """
        anchor = max(1, min(99, request.intended_price_cents))
        if request.side == "BUY":
            asks = [
                BookLevel(price_cents=min(100, anchor + k),
                          size_shares=max(1, request.intended_size_shares // 3))
                for k in range(5)
            ]
            # Best bid is just below anchor; irrelevant for a BUY walk.
            bids = [
                BookLevel(price_cents=max(0, anchor - 1 - k),
                          size_shares=max(1, request.intended_size_shares // 3))
                for k in range(5)
            ]
        else:
            asks = [
                BookLevel(price_cents=min(100, anchor + 1 + k),
                          size_shares=max(1, request.intended_size_shares // 3))
                for k in range(5)
            ]
            bids = [
                BookLevel(price_cents=max(0, anchor - k),
                          size_shares=max(1, request.intended_size_shares // 3))
                for k in range(5)
            ]
        return OrderbookSnapshot(
            market_id=request.market_id,
            outcome=request.outcome,
            asks=asks,
            bids=bids,
            timestamp=datetime.now(UTC),
        )


# ── pure helpers (exposed for tests) ────────────────────


def _walk_book(
    *,
    levels: Sequence[BookLevel],
    side: Side,
    target_shares: int,
    limit_price: int,
) -> tuple[int, int, list[BookLevel]]:
    """Return (filled_shares, notional_cents, levels_consumed)."""
    filled = 0
    notional = 0
    consumed: list[BookLevel] = []
    for level in levels:
        if side == "BUY" and level.price_cents > limit_price:
            break
        if side == "SELL" and level.price_cents < limit_price:
            break
        remaining = target_shares - filled
        if remaining <= 0:
            break
        take = min(level.size_shares, remaining)
        if take <= 0:
            continue
        filled += take
        notional += take * level.price_cents
        consumed.append(BookLevel(price_cents=level.price_cents, size_shares=take))
    return filled, notional, consumed


def _sample_lognormal(
    rng: random.Random,
    *,
    p50_ms: int,
    p95_ms: int,
) -> float:
    """Draw a positive sample from a log-normal whose median matches p50
    and whose 95th percentile matches p95.

    log(p50) = mu; log(p95) = mu + 1.6449*sigma (z for 95th percentile).
    Degenerate inputs collapse to p50 exactly (no randomness).
    """
    if p50_ms <= 0 or p95_ms <= p50_ms:
        return float(max(0, p50_ms))
    mu = math.log(p50_ms)
    sigma = (math.log(p95_ms) - mu) / 1.6449
    return max(0.0, math.exp(rng.gauss(mu, sigma)))


def _round_half_up(x: float) -> int:
    """Python's round() uses banker's rounding; fill-price math wants the
    classroom round-half-up behaviour so cost and fee are predictable."""
    return math.floor(x + 0.5)
