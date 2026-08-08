"""Kill-switch tests — build plan §4.5 / spec §14 #7."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from polysim.db import dao
from polysim.db.migrations.runner import apply_migrations
from polysim.models import Flag
from polysim.paper import kill_switches


@pytest.fixture
async def db(tmp_path: Path) -> Path:
    path = tmp_path / "t.db"
    await apply_migrations(path)
    return path


async def test_drawdown_switch_trips_past_threshold(db: Path) -> None:
    run_id = await dao.create_paper_run(
        db, name="t", starting_balance_cents=1_000_000, config_snapshot={}
    )
    # Simulate a 25% drawdown by reducing current balance to 750k.
    await dao.adjust_run_balance(db, run_id, -250_000)
    reason = await kill_switches.drawdown_switch(db, run_id, max_drawdown_pct=0.20)
    assert reason is not None
    assert reason.code == "drawdown"


async def test_drawdown_switch_quiet_when_under_limit(db: Path) -> None:
    run_id = await dao.create_paper_run(
        db, name="t", starting_balance_cents=1_000_000, config_snapshot={}
    )
    await dao.adjust_run_balance(db, run_id, -10_000)
    assert await kill_switches.drawdown_switch(db, run_id, max_drawdown_pct=0.20) is None


async def test_flag_rate_switch_trips_with_many_flags(db: Path) -> None:
    # Seed 150 flags in the last hour.
    await dao.upsert_wallet_first_sight(db, "0xaf4")
    from polysim.models import Market
    await dao.upsert_market(db, Market(
        id="m1", slug="s", question="Q?",
        created_at=datetime(2026, 4, 1, tzinfo=UTC),
    ))
    for i in range(150):
        await dao.write_flag(db, Flag(
            wallet_address="0xaf4", market_id="m1",
            detector_name="composite",
            raw_score=5.0, composite_score=8.0,
            components={},
            created_at=datetime.now(UTC) - timedelta(minutes=i % 60),
        ))
    reason = await kill_switches.flag_rate_switch(db, max_flags_per_hour=100)
    assert reason is not None
    assert reason.code == "flag_rate"


async def test_check_and_pause_marks_run(db: Path) -> None:
    run_id = await dao.create_paper_run(
        db, name="t", starting_balance_cents=1_000_000, config_snapshot={}
    )
    await dao.adjust_run_balance(db, run_id, -500_000)  # 50% drawdown

    reason = await kill_switches.check_and_pause(
        db, run_id, max_drawdown_pct=0.20, max_flags_per_hour=100
    )
    assert reason is not None

    row = await dao.get_paper_run(db, run_id)
    assert row is not None
    assert row["paused_at"] is not None
    assert "drawdown" in (row.get("pause_reason") or "")
