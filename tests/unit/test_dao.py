"""DAO tests — in-memory-ish SQLite, migrations applied."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from polysim.db import dao
from polysim.db.migrations.runner import apply_migrations
from polysim.models import Market, TradeEvent


@pytest.fixture
async def db(tmp_path: Path) -> Path:
    path = tmp_path / "t.db"
    await apply_migrations(path)
    return path


def _market(id_: str = "m1", category: str | None = None) -> Market:
    return Market(
        id=id_,
        slug=f"slug-{id_}",
        question=f"Question about {id_}?",
        category=category,  # type: ignore[arg-type]
        created_at=datetime(2026, 4, 10, tzinfo=UTC),
        resolves_at=datetime(2026, 9, 30, tzinfo=UTC),
        daily_volume_usd_cents=47_200_00,
    )


def _trade(id_: str, wallet: str = "0xaf4", market: str = "m1") -> TradeEvent:
    return TradeEvent(
        id=id_,
        wallet_address=wallet,
        market_id=market,
        side="BUY",
        outcome="YES",
        size_shares=150,
        price_cents=34,
        timestamp=datetime(2026, 4, 19, 14, 32, 11, tzinfo=UTC),
        tx_hash="0xabc",
    )


async def test_upsert_market_roundtrip(db: Path) -> None:
    await dao.upsert_market(db, _market(category="ai"))
    stats = await dao.db_stats(db)
    assert stats["markets"] == 1


async def test_upsert_market_idempotent(db: Path) -> None:
    await dao.upsert_market(db, _market(category="ai"))
    await dao.upsert_market(db, _market(category="aec"))  # category update attempt
    stats = await dao.db_stats(db)
    assert stats["markets"] == 1
    # category preserved (COALESCE ensures existing non-null wins)
    missing = await dao.get_markets_missing_category(db)
    assert missing == []


async def test_update_market_category(db: Path) -> None:
    await dao.upsert_market(db, _market(category=None))
    missing = await dao.get_markets_missing_category(db)
    assert len(missing) == 1
    await dao.update_market_category(db, "m1", "ai")
    assert await dao.get_markets_missing_category(db) == []


async def test_upsert_wallet_first_sight(db: Path) -> None:
    assert await dao.upsert_wallet_first_sight(db, "0xAF4") is True
    # Second insert returns False (already known).
    assert await dao.upsert_wallet_first_sight(db, "0xaf4") is False


async def test_upsert_wallet_enrichment(db: Path) -> None:
    await dao.upsert_wallet_first_sight(db, "0xaf4")
    await dao.upsert_wallet_enrichment(
        db,
        address="0xaf4",
        nonce=3,
        funding_source="binance",
        funding_first_deposit_at="2026-04-10T12:00:00+00:00",
        owner_address=None,
    )
    new_wallets = await dao.list_new_wallets(db)
    # After enrichment, the wallet is no longer in "new" set.
    assert all(w.address != "0xaf4" for w in new_wallets)


async def test_list_new_wallets_returns_only_unenriched(db: Path) -> None:
    await dao.upsert_wallet_first_sight(db, "0xaa1")
    await dao.upsert_wallet_first_sight(db, "0xbb2")
    await dao.upsert_wallet_enrichment(
        db, address="0xaa1", nonce=0,
        funding_source=None, funding_first_deposit_at=None,
    )
    new = await dao.list_new_wallets(db)
    assert [w.address for w in new] == ["0xbb2"]


async def test_insert_trades_batch_idempotent(db: Path) -> None:
    trades = [_trade("t1"), _trade("t2")]
    assert await dao.insert_trades_batch(db, trades) == 2
    # Second insert of same ids → 0 new
    assert await dao.insert_trades_batch(db, trades) == 0
    stats = await dao.db_stats(db)
    assert stats["trades"] == 2


async def test_insert_trades_creates_placeholder_market_and_wallet(db: Path) -> None:
    # Emit a trade before the market or wallet exists: placeholders are created.
    assert await dao.insert_trades_batch(db, [_trade("t1", wallet="0xnew", market="mnew")]) == 1
    stats = await dao.db_stats(db)
    assert stats["markets"] >= 1
    assert stats["wallets"] >= 1


async def test_record_clock_skews(db: Path) -> None:
    n = await dao.record_clock_skews(db, [100, -50, 250])
    assert n == 3


async def test_record_clock_skews_empty(db: Path) -> None:
    assert await dao.record_clock_skews(db, []) == 0
