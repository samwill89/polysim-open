"""Shared fixtures + constructors for scoring tests."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from polysim.models import Market, TradeEvent, WalletProfile


def wallet_profile(
    *,
    address: str = "0xaf4",
    total_markets: int = 10,
    resolved_markets: int = 10,
    wins: int = 8,
    losses: int = 2,
    categories: dict[str, int] | None = None,
    features: dict[str, Any] | None = None,
) -> WalletProfile:
    return WalletProfile(
        wallet_address=address,
        as_of=datetime(2026, 4, 19, 14, 0, tzinfo=UTC),
        total_markets=total_markets,
        resolved_markets=resolved_markets,
        wins=wins,
        losses=losses,
        win_rate=wins / max(1, wins + losses),
        total_pnl_cents=100_000,
        categories=categories or {"ai": 8, "aec": 2},
        category_exclusivity=0.68,
        avg_entry_to_resolution_hours=48.0,
        features=features or {},
    )


def market(
    *,
    id_: str = "m1",
    category: str | None = "ai",
    volume_cents: int | None = 47_200_00,
    resolves_at: datetime | None = None,
    resolved_outcome: str | None = None,
) -> Market:
    return Market(
        id=id_,
        slug=f"slug-{id_}",
        question="Will OpenAI release o4 by X?",
        category=category,  # type: ignore[arg-type]
        created_at=datetime(2026, 4, 10, tzinfo=UTC),
        resolves_at=resolves_at,
        resolved_outcome=resolved_outcome,  # type: ignore[arg-type]
        daily_volume_usd_cents=volume_cents,
    )


def trade(
    *,
    id_: str = "t1",
    wallet: str = "0xaf4",
    market_id: str = "m1",
    side: str = "BUY",
    outcome: str = "YES",
    size_shares: int = 4800,
    price_cents: int = 32,
    timestamp: datetime | None = None,
) -> TradeEvent:
    return TradeEvent(
        id=id_,
        wallet_address=wallet,
        market_id=market_id,
        side=side,  # type: ignore[arg-type]
        outcome=outcome,  # type: ignore[arg-type]
        size_shares=size_shares,
        price_cents=price_cents,
        timestamp=timestamp or datetime(2026, 4, 19, 13, 44, 1, tzinfo=UTC),
    )
