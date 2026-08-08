"""Evaluation hooks — realized-only lift measurement on synthetic rows."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from polysim.db import dao
from polysim.db.migrations.runner import apply_migrations
from polysim.models import Market
from polysim.signals.evaluation import (
    signal_bucket_outcomes,
    signal_coverage,
    signal_sizing_summary,
)
from polysim.signals.schema import MarketSignal
from polysim.signals.service import write_market_signal

TS = datetime(2026, 6, 20, tzinfo=UTC)


@pytest.fixture
async def db(tmp_path: Path) -> Path:
    path = tmp_path / "t.db"
    await apply_migrations(path)
    return path


async def _resolved_market(db: Path, mid: str) -> None:
    await dao.upsert_market(db, Market(
        id=mid, slug=mid, question=f"Q {mid}?",
        category="box_office",
        created_at=datetime(2026, 6, 1, tzinfo=UTC),
        resolved_outcome="YES",
        resolved_at=datetime(2026, 6, 25, tzinfo=UTC),
    ))


async def _position(
    db: Path, run_id: int, mid: str, *, pnl: int,
    mult: float | None = None, composite: float | None = None,
) -> None:
    pid = await dao.write_paper_position(
        db, run_id=run_id, market_id=mid, outcome="YES",
        size_shares=10, avg_entry_price_cents=40,
        source_flag_id=None, source_wallet="0xabc",
        signal_composite=composite, signal_multiplier=mult,
    )
    await dao.close_position(db, pid, realized_pnl_cents=pnl, status="RESOLVED")


async def test_bucket_outcomes_split_by_composite(db: Path) -> None:
    run_id = await dao.create_paper_run(
        db, name="r", starting_balance_cents=1_000_000, config_snapshot={},
    )
    # High-conviction market: winner. Low-conviction market: loser.
    await _resolved_market(db, "m-high")
    await _resolved_market(db, "m-low")
    await write_market_signal(db, MarketSignal(
        market_id="m-high", ts=TS, composite=0.8, confidence=0.9,
    ))
    await write_market_signal(db, MarketSignal(
        market_id="m-low", ts=TS, composite=0.2, confidence=0.9,
    ))
    await _position(db, run_id, "m-high", pnl=+600)
    await _position(db, run_id, "m-low", pnl=-400)

    buckets = {b["bucket"]: b for b in await signal_bucket_outcomes(db)}
    assert buckets["high"]["n_markets"] == 1
    assert buckets["high"]["realized_pnl_cents"] == 600
    assert buckets["high"]["win_rate"] == 1.0
    assert buckets["low"]["realized_pnl_cents"] == -400
    assert buckets["low"]["win_rate"] == 0.0


async def test_bucket_uses_last_signal_before_resolution(db: Path) -> None:
    run_id = await dao.create_paper_run(
        db, name="r", starting_balance_cents=1_000_000, config_snapshot={},
    )
    await _resolved_market(db, "m1")
    # Early low signal, later (still pre-resolution) high signal → 'high'.
    await write_market_signal(db, MarketSignal(
        market_id="m1", ts=datetime(2026, 6, 18, tzinfo=UTC),
        composite=0.1, confidence=0.9,
    ))
    await write_market_signal(db, MarketSignal(
        market_id="m1", ts=datetime(2026, 6, 24, tzinfo=UTC),
        composite=0.9, confidence=0.9,
    ))
    # Post-resolution signal must be ignored.
    await write_market_signal(db, MarketSignal(
        market_id="m1", ts=datetime(2026, 6, 28, tzinfo=UTC),
        composite=0.0, confidence=0.9,
    ))
    await _position(db, run_id, "m1", pnl=100)
    buckets = {b["bucket"]: b for b in await signal_bucket_outcomes(db)}
    assert "high" in buckets
    assert "low" not in buckets


async def test_sizing_summary_separates_adjusted(db: Path) -> None:
    run_id = await dao.create_paper_run(
        db, name="r", starting_balance_cents=1_000_000, config_snapshot={},
    )
    await _resolved_market(db, "m1")
    await _position(db, run_id, "m1", pnl=+200, mult=1.4, composite=0.8)
    await _position(db, run_id, "m1", pnl=-100)
    s = await signal_sizing_summary(db)
    assert s["adjusted"]["n"] == 1
    assert s["adjusted"]["realized_pnl_cents"] == 200
    assert s["adjusted"]["avg_multiplier"] == pytest.approx(1.4)
    assert s["untouched"]["n"] == 1
    assert s["untouched"]["realized_pnl_cents"] == -100


async def test_coverage_counts_open_markets_with_signals(db: Path) -> None:
    run_id = await dao.create_paper_run(
        db, name="r", starting_balance_cents=1_000_000, config_snapshot={},
    )
    await dao.write_paper_position(
        db, run_id=run_id, market_id="m-open", outcome="YES",
        size_shares=10, avg_entry_price_cents=40,
        source_flag_id=None, source_wallet="0xabc",
    )
    cov0 = await signal_coverage(db)
    assert cov0 == {"open_markets": 1, "with_any_signal": 0}
    await write_market_signal(db, MarketSignal(
        market_id="m-open", ts=TS, composite=0.6, confidence=0.7,
    ))
    cov1 = await signal_coverage(db)
    assert cov1 == {"open_markets": 1, "with_any_signal": 1}


async def test_empty_db_paths_return_empty(tmp_path: Path) -> None:
    missing = tmp_path / "nope.db"
    assert await signal_bucket_outcomes(missing) == []
    cov = await signal_coverage(missing)
    assert cov["open_markets"] == 0
    s = await signal_sizing_summary(missing)
    assert s["adjusted"]["n"] == 0
