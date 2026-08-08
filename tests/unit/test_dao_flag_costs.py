"""flag_costs DAO tests (G7)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from polysim.db import dao
from polysim.db.migrations.runner import apply_migrations
from polysim.models import Flag
from polysim.utils.time import iso


@pytest.fixture
async def db(tmp_path: Path) -> Path:
    path = tmp_path / "t.db"
    await apply_migrations(path)
    return path


async def _make_flag(db: Path, seq: int = 0) -> int:
    from datetime import timedelta

    f = Flag(
        wallet_address="0xaf4",
        market_id="m1",
        detector_name="composite",
        raw_score=5.0,
        composite_score=8.4,
        components={},
        # Unique timestamp per seq keeps the flags UNIQUE constraint happy.
        created_at=datetime(2026, 4, 19, 13, 44, tzinfo=UTC) + timedelta(seconds=seq),
    )
    # Pre-seed referenced wallet/market FKs (via the trade batch machinery).
    await dao.upsert_wallet_first_sight(db, "0xaf4")
    from polysim.models import Market
    await dao.upsert_market(
        db,
        Market(
            id="m1",
            slug="s",
            question="Q?",
            created_at=datetime(2026, 4, 10, tzinfo=UTC),
        ),
    )
    new_id = await dao.write_flag(db, f)
    assert new_id is not None
    return new_id


async def test_record_and_read_flag_cost(db: Path) -> None:
    flag_id = await _make_flag(db)
    await dao.record_flag_cost(
        db,
        flag_id,
        model="claude-opus-4-7",
        input_tokens=3200,
        output_tokens=380,
        cached_tokens=1500,
        cost_cents=78,
        latency_ms=4800,
    )
    row = await dao.get_flag_cost(db, flag_id)
    assert row is not None
    assert row["model"] == "claude-opus-4-7"
    assert row["cost_cents"] == 78
    assert row["input_tokens"] == 3200
    assert row["cached_tokens"] == 1500
    assert row["latency_ms"] == 4800


async def test_record_flag_cost_is_cumulative(db: Path) -> None:
    """Two calls on the same flag (rare, but possible) should add up."""
    flag_id = await _make_flag(db)
    await dao.record_flag_cost(
        db, flag_id, model="claude-haiku-4-5",
        input_tokens=100, output_tokens=50, cached_tokens=0, cost_cents=1,
    )
    await dao.record_flag_cost(
        db, flag_id, model="claude-opus-4-7",
        input_tokens=200, output_tokens=100, cached_tokens=50, cost_cents=9,
    )
    row = await dao.get_flag_cost(db, flag_id)
    assert row is not None
    assert row["input_tokens"] == 300
    assert row["output_tokens"] == 150
    assert row["cached_tokens"] == 50
    assert row["cost_cents"] == 10
    # Latest model wins
    assert row["model"] == "claude-opus-4-7"


async def test_get_flag_cost_missing(db: Path) -> None:
    assert await dao.get_flag_cost(db, 999) is None


async def test_sum_flag_costs_since(db: Path) -> None:
    # Three flags with three costs — distinct timestamps so the UNIQUE
    # constraint on (wallet, market, detector_name, created_at) doesn't
    # collapse them.
    for i in range(3):
        flag_id = await _make_flag(db, seq=i)
        await dao.record_flag_cost(
            db, flag_id, model="claude-haiku-4-5",
            input_tokens=100, output_tokens=50, cached_tokens=0, cost_cents=5,
        )
    summary = await dao.sum_flag_costs_since(
        db, since_iso=iso(datetime(2020, 1, 1, tzinfo=UTC))
    )
    assert summary["total_calls"] == 3
    assert summary["total_cost_cents"] == 15


async def test_sum_flag_costs_missing_db(tmp_path: Path) -> None:
    summary = await dao.sum_flag_costs_since(
        tmp_path / "nope.db",
        since_iso="2020-01-01T00:00:00Z",
    )
    assert summary == {"total_cost_cents": 0, "total_calls": 0, "total_cached_tokens": 0}
