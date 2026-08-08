"""Pricing arithmetic tests (Phase 3 §3.5 / G7)."""

from __future__ import annotations

import pytest

from polysim.investigator.pricing import (
    PRICING,
    cache_savings_cents,
    compute_cost_cents,
)


class TestComputeCostCents:
    def test_opus_uncached(self) -> None:
        # 1,000,000 input + 1,000,000 output on Opus at $15 / $75 per 1M.
        cost = compute_cost_cents(
            "claude-opus-4-7",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
        )
        # $15 + $75 = $90 -> 9000 cents
        assert cost == 9000

    def test_haiku_uncached(self) -> None:
        cost = compute_cost_cents(
            "claude-haiku-4-5",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
        )
        # $1 + $5 = $6 -> 600 cents
        assert cost == 600

    def test_cache_read_is_much_cheaper(self) -> None:
        """A 1M cache-read on Opus costs $1.50 vs $15 uncached input."""
        uncached = compute_cost_cents(
            "claude-opus-4-7", input_tokens=1_000_000, output_tokens=0
        )
        cached = compute_cost_cents(
            "claude-opus-4-7",
            input_tokens=0,
            output_tokens=0,
            cache_read_tokens=1_000_000,
        )
        assert cached * 10 == uncached  # exact 10x savings on cache reads

    def test_cache_write_slightly_above_uncached(self) -> None:
        """Cache-write is 1.25x input on Opus ($18.75 vs $15)."""
        uncached = compute_cost_cents(
            "claude-opus-4-7", input_tokens=1_000_000, output_tokens=0
        )
        write = compute_cost_cents(
            "claude-opus-4-7",
            input_tokens=0,
            output_tokens=0,
            cache_creation_tokens=1_000_000,
        )
        assert write > uncached

    def test_mixed_realistic_call(self) -> None:
        """A realistic investigator call (with cache hit)."""
        # 5000 input (prompt), 400 output, 2000 tokens read from cache.
        cost = compute_cost_cents(
            "claude-haiku-4-5",
            input_tokens=500,
            output_tokens=400,
            cache_creation_tokens=0,
            cache_read_tokens=4500,
        )
        # Expected: 500 * 1.0/1M + 400 * 5.0/1M + 4500 * 0.10/1M
        #         = 0.0005 + 0.002 + 0.00045 = 0.00295 -> 0.295 cents -> 0
        # Haiku is cheap — most single calls round to 0 cents.
        assert cost == 0  # sub-cent call rounds to 0

    def test_unknown_model_returns_zero(self) -> None:
        cost = compute_cost_cents(
            "gpt-5", input_tokens=1_000_000, output_tokens=1_000_000
        )
        assert cost == 0

    def test_alias_id_resolved(self) -> None:
        """Date-pinned Haiku alias maps to haiku-4-5 rates."""
        cost = compute_cost_cents(
            "claude-haiku-4-5-20251001",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
        )
        assert cost == 600

    @pytest.mark.parametrize("model", list(PRICING.keys()))
    def test_all_models_have_positive_rates(self, model: str) -> None:
        rates = PRICING[model]
        assert rates["input"] > 0
        assert rates["output"] > 0
        assert rates["cache_read"] < rates["input"]  # cache always cheaper
        assert rates["cache_write_5m"] > rates["input"]  # write always pricier


class TestCacheSavings:
    def test_zero_when_unknown(self) -> None:
        assert cache_savings_cents("gpt-5", cache_read_tokens=1_000_000) == 0

    def test_savings_positive_on_opus(self) -> None:
        # 1M cache-read on Opus saves (15 - 1.50) = $13.50 -> 1350 cents.
        assert cache_savings_cents("claude-opus-4-7", cache_read_tokens=1_000_000) == 1350
