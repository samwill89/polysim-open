"""Bid-price MTM tests — empirical-priors addendum §4.1.

Mid-price MTM overstates performance by the bid-ask spread (2-5c on
Polymarket). These tests lock in the bid-price contract and the
three-price ordering invariant (bid <= mid <= ask for uncrossed books).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from polysim.db.migrations.runner import apply_migrations
from polysim.portfolio.valuation import (
    best_bid_ask_from_snapshot,
    position_value_at_bid,
    value_position,
)


def test_position_value_uses_bid_not_mid() -> None:
    # Addendum §4.1 example: bought at 50¢ with bid=48/ask=52 → value == 48¢.
    val = value_position(
        position_id=1,
        size_shares=100,
        avg_entry_price_cents=50,
        bid_price_cents=48,
        ask_price_cents=52,
    )
    assert val.value_cents == 100 * 48                # bid-priced, not mid
    assert val.cost_cents == 100 * 50
    assert val.unrealized_pnl_cents == -200           # bid below entry → loss
    assert val.spread_cents == 4                      # 52 - 48


def test_position_value_monotone_in_size() -> None:
    a = value_position(
        position_id=1, size_shares=10,
        avg_entry_price_cents=40, bid_price_cents=42,
    )
    b = value_position(
        position_id=1, size_shares=100,
        avg_entry_price_cents=40, bid_price_cents=42,
    )
    assert b.value_cents > a.value_cents


def test_position_value_at_bid_clamps() -> None:
    assert position_value_at_bid(100, 50) == 5000
    assert position_value_at_bid(0, 50) == 0
    # Clamp: prices are cents in [0, 100]; callers should honor but be defensive.
    assert position_value_at_bid(100, -5) == 0
    assert position_value_at_bid(100, 999) == 100 * 100


@pytest.fixture
async def db(tmp_path: Path) -> Path:
    p = tmp_path / "t.db"
    await apply_migrations(p)
    return p


async def test_best_bid_ask_from_empty_db(tmp_path: Path) -> None:
    # No DB file at all → None.
    p = tmp_path / "nope.db"
    assert await best_bid_ask_from_snapshot(p, market_id="m1", outcome="YES") is None


async def test_best_bid_ask_from_snapshot_happy(db: Path) -> None:
    import aiosqlite
    async with aiosqlite.connect(str(db)) as conn:
        await conn.execute(
            "INSERT INTO orderbook_snapshots(market_id, outcome, timestamp, asks_json, bids_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                "m1", "YES", "2026-04-20T12:00:00+00:00",
                json.dumps([{"price_cents": 52, "size_shares": 100},
                            {"price_cents": 54, "size_shares": 200}]),
                json.dumps([{"price_cents": 48, "size_shares": 150},
                            {"price_cents": 46, "size_shares": 300}]),
            ),
        )
        await conn.commit()
    got = await best_bid_ask_from_snapshot(db, market_id="m1", outcome="YES")
    assert got == (48, 52)


async def test_best_bid_ask_crossed_book_returns_none(db: Path) -> None:
    """If bid > ask (crossed/malformed), we return None to force a
    fallback rather than producing a nonsensical mark."""
    import aiosqlite
    async with aiosqlite.connect(str(db)) as conn:
        await conn.execute(
            "INSERT INTO orderbook_snapshots(market_id, outcome, timestamp, asks_json, bids_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                "m1", "YES", "2026-04-20T12:00:00+00:00",
                json.dumps([{"price_cents": 40, "size_shares": 10}]),  # ask below bid
                json.dumps([{"price_cents": 60, "size_shares": 10}]),
            ),
        )
        await conn.commit()
    assert await best_bid_ask_from_snapshot(db, market_id="m1", outcome="YES") is None


def test_bid_leq_mid_leq_ask_invariant() -> None:
    """The three-price ordering is what bid-MTM relies on.

    Property: for any uncrossed book, bid-priced value ≤ mid-priced value
    ≤ ask-priced value. We check our function respects this ordering
    by construction — valuing at bid never exceeds valuing at any higher
    mark-quote.
    """
    for bid, mid, ask in [(40, 45, 50), (1, 50, 99), (30, 35, 40)]:
        assert bid <= mid <= ask  # fixture sanity
        v_bid = position_value_at_bid(100, bid)
        v_mid_hypothetical = 100 * mid
        v_ask_hypothetical = 100 * ask
        assert v_bid <= v_mid_hypothetical <= v_ask_hypothetical
