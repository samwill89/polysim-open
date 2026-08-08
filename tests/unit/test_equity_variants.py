"""Equity variant selection tests."""

from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest

from polysim.db import dao
from polysim.db.migrations.runner import apply_migrations
from polysim.equity.executor import EquityExecutor
from polysim.equity.loop import EquityTrackLoop
from polysim.equity.variants import (
    DEFAULT_LIVE_VARIANTS,
    EQUITY_VARIANTS,
    select_variants,
)


@pytest.fixture
async def db(tmp_path: Path) -> Path:
    path = tmp_path / "t.db"
    await apply_migrations(path)
    return path


def test_select_variants_preserves_requested_live_set() -> None:
    selected = select_variants(DEFAULT_LIVE_VARIANTS)
    assert [v.name for v in selected] == list(DEFAULT_LIVE_VARIANTS)


def test_live_set_is_benchmark_plus_slow_momentum() -> None:
    assert DEFAULT_LIVE_VARIANTS == ("smh_bh", "momentum_slow")
    selected = {
        variant.name: variant
        for variant in select_variants(DEFAULT_LIVE_VARIANTS)
    }
    assert selected["smh_bh"].symbols == ("SMH",)
    assert selected["momentum_slow"].trail_sma == 50
    assert selected["momentum_slow"].time_stop_days == 63


def test_select_variants_none_returns_full_research_set() -> None:
    selected = select_variants(None)
    assert [v.name for v in selected] == [v.name for v in EQUITY_VARIANTS]


def test_select_variants_rejects_unknown_name() -> None:
    with pytest.raises(ValueError, match="unknown equity variant"):
        select_variants(["momentum", "not_a_variant"])


async def test_equity_loop_seeds_only_requested_variants(db: Path) -> None:
    loop = EquityTrackLoop(
        db,
        balance_cents=1_000_000,
        variant_names=["ew_bh", "momentum"],
    )

    created = await loop.seed_runs()
    assert created == 2

    rows = await dao.list_paper_runs_by_tag(db, tag="equity_v1")
    assert {r["name"] for r in rows} == {"equity-ew_bh", "equity-momentum"}
    assert {r["profile_name"] for r in rows} == {"ew_bh", "momentum"}


async def test_equity_loop_ends_unselected_open_runs(db: Path) -> None:
    old_id = await dao.create_paper_run(
        db,
        name="equity-contrarian",
        starting_balance_cents=1_000_000,
        config_snapshot={},
        profile_name="contrarian",
        tag="equity_v1",
    )
    loop = EquityTrackLoop(
        db,
        balance_cents=1_000_000,
        variant_names=["smh_bh", "momentum_slow"],
    )
    assert await loop.seed_runs() == 2
    old = await dao.get_paper_run(db, old_id)
    assert old is not None
    assert old["ended_at"] is not None
    assert "strategy_set_v2" in str(old["notes"])


async def test_single_symbol_buy_and_hold_spends_all_cash(db: Path) -> None:
    run_id = await dao.create_paper_run(
        db,
        name="equity-smh_bh",
        starting_balance_cents=1_000_000,
        config_snapshot={},
        profile_name="smh_bh",
        tag="equity_v1",
    )
    async with aiosqlite.connect(str(db)) as conn:
        await conn.execute(
            "INSERT INTO equity_quotes("
            "ticker, date, open_cents, high_cents, low_cents, close_cents, "
            "volume, source, fetched_at) VALUES ("
            "'SMH', '2026-07-09', 66800, 67000, 66500, 66891, "
            "1000, 'test', '2026-07-10T00:00:00+00:00')"
        )
        await conn.commit()

    variant = select_variants(["smh_bh"])[0]
    result = await EquityExecutor(db, run_id=run_id, variant=variant).tick(
        ["SMH"], day="2026-07-09"
    )

    assert result["opened"] == 1
    async with aiosqlite.connect(str(db)) as conn:
        position = await conn.execute_fetchall(
            "SELECT ticker, status, shares FROM equity_positions WHERE run_id = ?",
            (run_id,),
        )
        cash = await conn.execute_fetchall(
            "SELECT cash_cents FROM equity_run_state WHERE run_id = ?",
            (run_id,),
        )
    assert position[0][0:2] == ("SMH", "OPEN")
    assert float(position[0][2]) > 0
    assert cash == [(0,)]
