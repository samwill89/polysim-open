"""Uptime watchdog tests — build plan §6.5."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from polysim.db import dao
from polysim.db.migrations.runner import apply_migrations
from polysim.models import TradeEvent
from polysim.utils.watchdog import UptimeWatchdog, stall_seconds


@pytest.fixture
async def db(tmp_path: Path) -> Path:
    path = tmp_path / "t.db"
    await apply_migrations(path)
    return path


def test_stall_seconds_basic() -> None:
    now = datetime(2026, 4, 19, 10, 0, tzinfo=UTC)
    assert stall_seconds(None, now) is None
    gap = stall_seconds(now - timedelta(seconds=120), now)
    assert gap is not None
    assert 119 <= gap <= 121


async def _seed_trade(db: Path, *, ts: datetime) -> None:
    await dao.insert_trades_batch(
        db,
        [
            TradeEvent(
                id=f"t-{int(ts.timestamp())}",
                wallet_address="0xaf4",
                market_id="m1",
                side="BUY",
                outcome="YES",
                size_shares=10,
                price_cents=40,
                timestamp=ts,
            )
        ],
    )


async def test_no_fire_when_fresh(db: Path) -> None:
    await _seed_trade(db, ts=datetime.now(UTC) - timedelta(seconds=5))
    calls: list[str] = []

    async def fake_alert(msg: str) -> None:
        calls.append(msg)

    wd = UptimeWatchdog(db, alert_fn=fake_alert, stall_threshold_s=60)
    assert await wd.check_once() is False
    assert calls == []


async def test_fires_when_stale(db: Path) -> None:
    await _seed_trade(db, ts=datetime.now(UTC) - timedelta(minutes=10))
    calls: list[str] = []

    async def fake_alert(msg: str) -> None:
        calls.append(msg)

    wd = UptimeWatchdog(db, alert_fn=fake_alert, stall_threshold_s=60)
    assert await wd.check_once() is True
    assert len(calls) == 1
    assert "stalled" in calls[0].lower()


async def test_does_not_re_fire_within_rearm(db: Path) -> None:
    await _seed_trade(db, ts=datetime.now(UTC) - timedelta(minutes=10))
    calls: list[str] = []

    async def fake_alert(msg: str) -> None:
        calls.append(msg)

    wd = UptimeWatchdog(
        db, alert_fn=fake_alert, stall_threshold_s=60, rearm_s=3600
    )
    assert await wd.check_once() is True
    # Second check, still stale, but within rearm window -> no extra alert.
    assert await wd.check_once() is False
    assert len(calls) == 1


async def test_no_fire_when_db_empty(db: Path) -> None:
    calls: list[str] = []

    async def fake_alert(msg: str) -> None:
        calls.append(msg)

    wd = UptimeWatchdog(db, alert_fn=fake_alert, stall_threshold_s=60)
    # Empty DB -> no fire (nothing to be stale about yet).
    assert await wd.check_once() is False
