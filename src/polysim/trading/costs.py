"""Current Polymarket protocol-fee calculations for paper fills."""

from __future__ import annotations

from decimal import ROUND_CEILING, Decimal
from typing import Any

_FEE_RATES = {
    "crypto": Decimal("0.07"),
    "sports": Decimal("0.05"),
    "finance": Decimal("0.04"),
    "politics": Decimal("0.04"),
    "economics": Decimal("0.05"),
    "culture": Decimal("0.05"),
    "pop_culture": Decimal("0.05"),
    "weather": Decimal("0.05"),
    "other": Decimal("0.05"),
    "tech": Decimal("0.04"),
    "mentions": Decimal("0.04"),
    "geopolitics": Decimal("0"),
}


def market_fees_enabled(metadata: dict[str, Any] | None) -> bool:
    if not metadata:
        return False
    value = metadata.get("feesEnabled", metadata.get("fees_enabled"))
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() == "true"


def taker_fee_cents(
    *,
    shares: int,
    price_cents: int,
    category: str | None,
    metadata: dict[str, Any] | None = None,
    fees_enabled: bool | None = None,
) -> int:
    """Apply the persisted per-market curve, falling back to current category rates."""
    enabled = market_fees_enabled(metadata) if fees_enabled is None else fees_enabled
    if not enabled or shares <= 0 or price_cents <= 0 or price_cents >= 100:
        return 0
    rate, exponent = _fee_parameters(metadata, category)
    price = Decimal(price_cents) / 100
    curve = (price * (1 - price)) ** exponent
    fee_usdc = Decimal(shares) * rate * curve
    # The paper ledger stores whole cents while the venue supports finer fee
    # precision. Round positive fees up so the simulation cannot gain edge
    # merely from its coarser accounting unit.
    return int((fee_usdc * 100).quantize(Decimal("1"), rounding=ROUND_CEILING))


def _fee_parameters(
    metadata: dict[str, Any] | None,
    category: str | None,
) -> tuple[Decimal, int]:
    schedule: Any = None
    if metadata:
        schedule = metadata.get("feeSchedule", metadata.get("fee_schedule"))
    if isinstance(schedule, dict):
        try:
            rate = Decimal(str(schedule.get("rate")))
            exponent = int(schedule.get("exponent", 1))
            if rate >= 0 and 1 <= exponent <= 8:
                return rate, exponent
        except (ArithmeticError, TypeError, ValueError):
            pass
    return _FEE_RATES.get(str(category or "other").lower(), Decimal("0.05")), 1


__all__ = ["market_fees_enabled", "taker_fee_cents"]
