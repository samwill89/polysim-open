"""Bid-price mark-to-market — empirical-priors addendum §4.1.

Mid-price MTM systematically overstates performance by the bid-ask spread
(2-5c typical on Polymarket). Every account-value calculation routes
through this module and uses bid prices for open positions.

The CI safety grep bans the Python identifier for the forbidden form
in this file specifically; that's the enforcement layer. Call sites
pass the bid explicitly or via the orderbook snapshot helper — no
midpoint path exists.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PositionValuation:
    """Bid-priced value of one position."""

    position_id: int
    size_shares: int
    avg_entry_price_cents: int
    bid_price_cents: int              # the mark — NOT mid
    value_cents: int                  # size * bid
    cost_cents: int                   # size * avg_entry
    unrealized_pnl_cents: int         # value - cost
    spread_cents: int                 # ask - bid (for cost-accounting audits)


def position_value_at_bid(
    size_shares: int, bid_price_cents: int
) -> int:
    """Current value of an open position under bid-price MTM.

    This is the *only* sanctioned way to mark an open position. Mid- or
    last-trade-based valuations are forbidden by §4.1 because they
    systematically overstate performance.
    """
    if size_shares <= 0:
        return 0
    bid = max(0, min(100, int(bid_price_cents)))
    return int(size_shares) * bid


def value_position(
    *,
    position_id: int,
    size_shares: int,
    avg_entry_price_cents: int,
    bid_price_cents: int,
    ask_price_cents: int | None = None,
) -> PositionValuation:
    cost = int(size_shares) * int(avg_entry_price_cents)
    value = position_value_at_bid(size_shares, bid_price_cents)
    spread = 0
    if ask_price_cents is not None and ask_price_cents >= bid_price_cents:
        spread = int(ask_price_cents) - int(bid_price_cents)
    return PositionValuation(
        position_id=position_id,
        size_shares=size_shares,
        avg_entry_price_cents=avg_entry_price_cents,
        bid_price_cents=bid_price_cents,
        value_cents=value,
        cost_cents=cost,
        unrealized_pnl_cents=value - cost,
        spread_cents=spread,
    )


async def best_bid_ask_from_snapshot(
    db_path: Path, *, market_id: str, outcome: str
) -> tuple[int, int] | None:
    """Read the newest orderbook snapshot for (market, outcome) and
    return (best_bid_cents, best_ask_cents), or None if unavailable.

    Used by `value_position` callers who want to auto-fetch the bid.
    """
    if not db_path.exists():
        return None
    try:
        async with aiosqlite.connect(str(db_path)) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT asks_json, bids_json FROM orderbook_snapshots "
                "WHERE market_id = ? AND outcome = ? "
                "ORDER BY timestamp DESC LIMIT 1",
                (market_id, outcome),
            ) as cur:
                row = await cur.fetchone()
    except aiosqlite.OperationalError:
        return None
    if row is None:
        return None
    try:
        asks: list[dict[str, Any]] = json.loads(row["asks_json"])
        bids: list[dict[str, Any]] = json.loads(row["bids_json"])
    except (json.JSONDecodeError, TypeError):
        return None
    if not asks or not bids:
        return None
    try:
        best_ask = int(asks[0]["price_cents"])
        best_bid = int(bids[0]["price_cents"])
    except (KeyError, ValueError, TypeError):
        return None
    if best_bid > best_ask:
        # Crossed book — treat as no reliable mark.
        return None
    return best_bid, best_ask


__all__ = [
    "PositionValuation",
    "best_bid_ask_from_snapshot",
    "position_value_at_bid",
    "value_position",
]
