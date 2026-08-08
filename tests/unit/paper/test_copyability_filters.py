"""Copyability gates for selective CohortCopy tournament variants."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from polysim.config import BankrollConfig, RiskProfile
from polysim.db import dao
from polysim.db.migrations.runner import apply_migrations
from polysim.models import Flag, Market, TradeEvent
from polysim.paper.fill_model import FillModel
from polysim.paper.profile_executor import ProfilePaperExecutor
from polysim.profiles import load_profile


@pytest.fixture
async def db(tmp_path: Path) -> Path:
    path = tmp_path / "copyability.db"
    await apply_migrations(path)
    return path


async def seed_flag(
    db: Path,
    *,
    category: str = "sports",
    price_cents: int = 40,
    size_shares: int = 500,
    volume_cents: int = 10_000_000,
) -> int:
    market_id = f"m-{category}-{price_cents}-{size_shares}-{volume_cents}"
    await dao.upsert_market(
        db,
        Market(
            id=market_id,
            slug=market_id,
            question="Will the event happen?",
            category=category,  # type: ignore[arg-type]
            created_at=datetime(2026, 7, 1, tzinfo=UTC),
            daily_volume_usd_cents=volume_cents,
        ),
    )
    await dao.upsert_wallet_first_sight(db, "0xcafe")
    trade_id = f"trade-{market_id}"
    await dao.insert_trades_batch(
        db,
        [
            TradeEvent(
                id=trade_id,
                wallet_address="0xcafe",
                market_id=market_id,
                side="BUY",
                outcome="YES",
                size_shares=size_shares,
                price_cents=price_cents,
                timestamp=datetime.now(UTC),
            )
        ],
    )
    flag_id = await dao.write_flag(
        db,
        Flag(
            wallet_address="0xcafe",
            market_id=market_id,
            trade_id=trade_id,
            detector_name="CohortCopy",
            raw_score=10.0,
            composite_score=10.0,
            components={"contributing_detectors": ["CohortCopy"]},
            created_at=datetime.now(UTC),
        ),
    )
    assert flag_id is not None
    return flag_id


async def executor(db: Path, profile: RiskProfile) -> ProfilePaperExecutor:
    run_id = await dao.create_paper_run(
        db,
        name=f"selective-{id(profile)}",
        starting_balance_cents=1_000_000,
        config_snapshot={},
        profile_name=profile.name,
        profile_snapshot=profile.model_dump(),
    )
    return ProfilePaperExecutor(
        db,
        run_id=run_id,
        profile=profile,
        bankroll=BankrollConfig(),
        fill_model=FillModel(),
    )


def selective_profile() -> RiskProfile:
    return load_profile("systematic").model_copy(
        update={
            "allowed_market_categories": ["sports", "politics"],
            "min_source_trade_notional_cents": 10_000,
            "max_entry_price_cents": 70,
            "min_market_daily_volume_cents": 5_000_000,
        }
    )


async def test_copyability_filters_accept_qualifying_trade(db: Path) -> None:
    flag_id = await seed_flag(db)
    ex = await executor(db, selective_profile())
    assert await ex.consider_flag(flag_id) is not None


@pytest.mark.parametrize(
    ("kwargs"),
    [
        {"size_shares": 100},
        {"price_cents": 71},
        {"category": "ai"},
        {"volume_cents": 1_000_000},
    ],
)
async def test_copyability_filters_reject_nonqualifying_trade(
    db: Path, kwargs: dict[str, object]
) -> None:
    flag_id = await seed_flag(db, **kwargs)  # type: ignore[arg-type]
    ex = await executor(db, selective_profile())
    assert await ex.consider_flag(flag_id) is None
