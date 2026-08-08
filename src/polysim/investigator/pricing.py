"""Anthropic model pricing table + cost-cents calculator.

Rates are USD per 1M tokens, public pricing page as of 2026-04. When
Anthropic adjusts prices, update this table — everything else flows
through `compute_cost_cents()`.

Spec §14 #6 isn't relevant here (pricing doesn't feed the scorer), but
§7.5 "Cost guardrail" and build plan G7 rely on accurate accounting.
"""

from __future__ import annotations

from typing import TypedDict


class ModelRates(TypedDict):
    input: float        # uncached input token rate (USD / 1M)
    output: float
    cache_write_5m: float
    cache_read: float


# Canonical model IDs. We accept the short aliases (claude-opus-4-7) AND
# the date-pinned forms Anthropic sometimes requires in the SDK.
PRICING: dict[str, ModelRates] = {
    "claude-opus-4-7": {
        "input": 15.00,
        "output": 75.00,
        "cache_write_5m": 18.75,
        "cache_read": 1.50,
    },
    "claude-haiku-4-5": {
        "input": 1.00,
        "output": 5.00,
        "cache_write_5m": 1.25,
        "cache_read": 0.10,
    },
}

# Aliases — date-pinned IDs map to the same rate card.
_ALIASES: dict[str, str] = {
    "claude-haiku-4-5-20251001": "claude-haiku-4-5",
}


def _resolve_model_key(model: str) -> str | None:
    """Return the canonical key in PRICING, or None if the model is unknown."""
    if model in PRICING:
        return model
    if model in _ALIASES:
        return _ALIASES[model]
    # Permissive: if the model starts with a known family, fall through.
    for key in PRICING:
        if model.startswith(key):
            return key
    return None


def compute_cost_cents(
    model: str,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_creation_tokens: int = 0,
    cache_read_tokens: int = 0,
) -> int:
    """Return USD cents (rounded) for a single Claude API call.

    Anthropic's API convention: ``input_tokens`` is the uncached portion
    of the input; ``cache_creation_tokens`` + ``cache_read_tokens`` are
    additional buckets. Total input billed = sum of all three at their
    respective rates.
    """
    key = _resolve_model_key(model)
    if key is None:
        return 0  # unknown model → don't account, don't crash
    rates = PRICING[key]
    total_usd = (
        input_tokens * rates["input"] / 1_000_000.0
        + output_tokens * rates["output"] / 1_000_000.0
        + cache_creation_tokens * rates["cache_write_5m"] / 1_000_000.0
        + cache_read_tokens * rates["cache_read"] / 1_000_000.0
    )
    return round(total_usd * 100.0)


def cache_savings_cents(
    model: str,
    *,
    cache_read_tokens: int,
) -> int:
    """How much we'd have paid had these tokens NOT been cache-reads."""
    key = _resolve_model_key(model)
    if key is None:
        return 0
    rates = PRICING[key]
    delta_usd = (
        cache_read_tokens * (rates["input"] - rates["cache_read"]) / 1_000_000.0
    )
    return round(delta_usd * 100.0)
