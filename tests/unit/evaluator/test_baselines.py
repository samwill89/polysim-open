"""Baseline runner tests — build plan §5.4."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from polysim.db import dao
from polysim.db.migrations.runner import apply_migrations
from polysim.evaluator import baselines
from polysim.models import Market


@pytest.fixture
async def db(tmp_path: Path) -> Path:
    path = tmp_path / "t.db"
    await apply_migrations(path)
    return path


async def _seed_primary_run(db: Path, *, n_positions: int = 3) -> int:
    """Create a tiny primary run with N closed positions, all on resolved markets."""
    run_id = await dao.create_paper_run(
        db,
        name="primary",
        starting_balance_cents=1_000_000,
        config_snapshot={},
    )
    base = datetime(2026, 4, 1, tzinfo=UTC)
    for i in range(n_positions):
        market_id = f"m{i}"
        await dao.upsert_market(
            db,
            Market(
                id=market_id,
                slug=market_id,
                question="Q?",
                category="ai",  # type: ignore[arg-type]
                created_at=base,
                resolves_at=base + timedelta(days=7),
                resolved_outcome="YES",  # type: ignore[arg-type]
                resolved_at=base + timedelta(days=7, hours=1),
            ),
        )
        pos_id = await dao.write_paper_position(
            db, run_id=run_id, market_id=market_id, outcome="YES",
            size_shares=100, avg_entry_price_cents=40,
            source_flag_id=None, source_wallet=f"0xwallet{i}",
        )
        await dao.close_position(
            db, pos_id, realized_pnl_cents=60 * 100, status="RESOLVED",
        )
    return run_id


async def test_null_baseline_reproducible(db: Path) -> None:
    run_id = await _seed_primary_run(db)

    a = await baselines.run_null_baseline(
        db, primary_run_id=run_id, starting_balance_cents=1_000_000, seed=42
    )
    b = await baselines.run_null_baseline(
        db, primary_run_id=run_id, starting_balance_cents=1_000_000, seed=42
    )
    # Both runs should have closed positions with identical outcomes.
    rows_a = await dao.list_open_positions(db, a)  # after close they're closed, not open
    _ = rows_a
    import aiosqlite
    async with aiosqlite.connect(str(db)) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT outcome FROM paper_positions WHERE run_id = ? ORDER BY id", (a,)
        ) as cur:
            outcomes_a = [r["outcome"] for r in await cur.fetchall()]
        async with conn.execute(
            "SELECT outcome FROM paper_positions WHERE run_id = ? ORDER BY id", (b,)
        ) as cur:
            outcomes_b = [r["outcome"] for r in await cur.fetchall()]
    assert outcomes_a == outcomes_b
    assert len(outcomes_a) == 3


async def test_favorite_baseline_always_yes(db: Path) -> None:
    run_id = await _seed_primary_run(db)
    fav = await baselines.run_favorite_baseline(
        db, primary_run_id=run_id, starting_balance_cents=1_000_000,
    )

    import aiosqlite
    async with aiosqlite.connect(str(db)) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT outcome FROM paper_positions WHERE run_id = ?", (fav,)
        ) as cur:
            outcomes = [r["outcome"] for r in await cur.fetchall()]
    assert all(o == "YES" for o in outcomes)


async def test_baseline_raises_when_no_primary_positions(db: Path) -> None:
    run_id = await dao.create_paper_run(
        db, name="empty", starting_balance_cents=1_000_000, config_snapshot={}
    )
    with pytest.raises(ValueError):
        await baselines.run_null_baseline(
            db, primary_run_id=run_id, starting_balance_cents=1_000_000,
        )
    with pytest.raises(ValueError):
        await baselines.run_favorite_baseline(
            db, primary_run_id=run_id, starting_balance_cents=1_000_000,
        )
