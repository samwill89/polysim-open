"""Wallet profiler — idempotent recompute of per-wallet snapshots.

Spec §7.2. Build plan §2.1 (compute) + §2.10 (G6 staleness).

Profile = pure function of (wallet_trades, market_metadata).  Re-running on
the same inputs produces the same WalletProfile (spec §14 #5 corollary).

Stylized per-trade PnL model — sufficient for detector inputs, not for
paper execution accounting:

  buy  X @ p, resolved in favor → pnl = size * (100 - p) cents
  buy  X @ p, resolved against  → pnl = -size * p
  sell X @ p, resolved in favor → pnl = -size * (100 - p)   (short, lost)
  sell X @ p, resolved against  → pnl =  size * p           (short, won)
  invalid market                → pnl = 0   (spec §9)

This treats each trade as independent. A more accurate model (aggregate
per (wallet, market, outcome) with net position + avg cost) is Phase 5
work — detectors only need counts + directional PnL, not portfolio math.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import timedelta
from pathlib import Path

from polysim.db import dao
from polysim.models import Market, TradeEvent, WalletProfile
from polysim.utils.time import now_utc

log = logging.getLogger(__name__)

STALENESS_WINDOW = timedelta(hours=1)  # G6 — profiles >1h are stale


def compute(
    wallet_address: str, trades_and_markets: list[tuple[TradeEvent, Market]]
) -> WalletProfile:
    """Pure — given a wallet's trades + their markets, build a profile."""
    markets_seen: set[str] = set()
    resolved_market_ids: set[str] = set()
    wins = 0
    losses = 0
    total_pnl_cents = 0
    categories: Counter[str] = Counter()
    holding_hours: list[float] = []

    for trade, market in trades_and_markets:
        markets_seen.add(market.id)
        if market.category:
            categories[market.category] += 1

        if market.resolved_outcome is None:
            continue  # unresolved — do not contribute to W/L
        resolved_market_ids.add(market.id)

        pnl = _per_trade_pnl(trade, market)
        total_pnl_cents += pnl
        if pnl > 0:
            wins += 1
        elif pnl < 0:
            losses += 1

        if market.resolved_at is not None:
            hours = (market.resolved_at - trade.timestamp).total_seconds() / 3600.0
            if hours > 0:
                holding_hours.append(hours)

    total_markets = len(markets_seen)
    resolved_markets = len(resolved_market_ids)
    settled_trades = wins + losses
    win_rate = wins / settled_trades if settled_trades > 0 else 0.0
    avg_hours = (
        sum(holding_hours) / len(holding_hours) if holding_hours else 0.0
    )

    # Herfindahl-Hirschman index on category distribution: 1.0 means
    # fully concentrated in one category, near-zero = spread evenly.
    exclusivity = 0.0
    total_cats = sum(categories.values())
    if total_cats > 0:
        exclusivity = sum((n / total_cats) ** 2 for n in categories.values())

    return WalletProfile(
        wallet_address=wallet_address.lower(),
        as_of=now_utc(),
        total_markets=total_markets,
        resolved_markets=resolved_markets,
        wins=wins,
        losses=losses,
        win_rate=win_rate,
        total_pnl_cents=total_pnl_cents,
        categories=dict(categories),
        category_exclusivity=exclusivity,
        avg_entry_to_resolution_hours=avg_hours,
        features={},
    )


def _per_trade_pnl(trade: TradeEvent, market: Market) -> int:
    if market.resolved_outcome is None or market.resolved_outcome == "INVALID":
        return 0
    payout = 100 if market.resolved_outcome == trade.outcome else 0
    if trade.side == "BUY":
        return trade.size_shares * (payout - trade.price_cents)
    return trade.size_shares * (trade.price_cents - payout)


def is_stale(profile: WalletProfile | None) -> bool:
    """Staleness guard — spec gap G6. Scorer must refuse stale profiles."""
    if profile is None:
        return True
    return (now_utc() - profile.as_of) > STALENESS_WINDOW


# ── DB-facing orchestration ──────────────────────────────


async def recompute_for_wallet(
    db_path: Path, wallet_address: str
) -> WalletProfile:
    """Fetch + compute + write in one step."""
    pairs = await dao.get_trades_with_market_resolution(db_path, wallet_address)
    profile = compute(wallet_address, pairs)
    await dao.write_wallet_profile(db_path, profile)
    return profile


async def refresh_stale_profiles(
    db_path: Path, *, staleness_seconds: int = 60, max_wallets: int = 500
) -> int:
    """Recompute profiles for wallets whose last snapshot is older than
    the threshold, up to max_wallets. Returns count refreshed.

    Call this from a background task every N seconds, or when trade-count
    per wallet crosses a batch threshold.
    """
    addresses = await dao.list_wallets_needing_profile(
        db_path, staleness_seconds=staleness_seconds, limit=max_wallets
    )
    for addr in addresses:
        try:
            await recompute_for_wallet(db_path, addr)
        except Exception as exc:
            log.warning("profile recompute failed for %s: %s", addr, exc)
    return len(addresses)
