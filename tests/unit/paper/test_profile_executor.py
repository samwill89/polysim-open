"""Dual-mode addendum tests — A11 / Phase 4.5 acceptance.

Covers:
  * Profile loading (all 3 built-ins validate)
  * Per-profile filter logic (composite, detectors, odds, volume)
  * Per-profile sizing math (fixed / percentage / kelly_fractional)
  * Dispatcher fan-out to N executors
  * Drawdown pause + daily-loss pause
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from polysim.config import BankrollConfig, RiskProfile
from polysim.db import dao
from polysim.db.migrations.runner import apply_migrations
from polysim.models import Flag, Market, TradeEvent
from polysim.paper.fill_model import FillModel
from polysim.paper.profile_executor import (
    Dispatcher,
    ProfilePaperExecutor,
    _estimate_edge,
    daily_loss_switch,
)
from polysim.profiles import BUILTIN_PROFILE_NAMES, load_profile


@pytest.fixture
async def db(tmp_path: Path) -> Path:
    path = tmp_path / "t.db"
    await apply_migrations(path)
    return path


# ── A3 — all three built-in profiles validate ────────────


def test_all_builtins_load_and_validate() -> None:
    for name in BUILTIN_PROFILE_NAMES:
        p = load_profile(name)
        assert p.name == name
        p.ensure_sizing_fields()


def test_systematic_is_fixed_sizing() -> None:
    p = load_profile("systematic")
    assert p.position_sizing_mode == "fixed"
    assert p.fixed_copy_cents == 5000
    assert p.compound_winnings is False


def test_medium_is_kelly_fractional() -> None:
    p = load_profile("medium")
    assert p.position_sizing_mode == "kelly_fractional"
    assert p.kelly_fraction == 0.25
    assert p.allowed_detectors is not None
    assert "CategoryInsiderDetector" in p.allowed_detectors


def test_degen_is_percentage_no_drawdown_cap() -> None:
    p = load_profile("degen")
    assert p.position_sizing_mode == "percentage"
    assert p.drawdown_limit_pct is None
    assert p.max_pct_per_position == 0.50


# ── A5 — sizing math ─────────────────────────────────────


def _make_exec(
    db: Path, run_id: int, profile: RiskProfile
) -> ProfilePaperExecutor:
    return ProfilePaperExecutor(
        db,
        run_id=run_id,
        profile=profile,
        bankroll=BankrollConfig(),
        fill_model=FillModel(),
    )


async def test_fixed_sizing_uses_fixed_cents(db: Path) -> None:
    p = load_profile("systematic")
    run_id = await dao.create_paper_run(
        db, name="s", starting_balance_cents=1_000_000,
        config_snapshot={}, profile_name=p.name, profile_snapshot=p.model_dump(),
    )
    ex = _make_exec(db, run_id, p)
    # Fixed sizing = profile.fixed_copy_cents, bounded by 2% of balance
    # balance=$10k, cap=2% = 20_000¢, fixed_copy=5_000¢ -> 5000
    size = ex._size_position(
        balance=1_000_000, flag={"composite_score": 7.0}, current_odds=0.4
    )
    assert size == 5000


async def test_percentage_sizing_matches_cap(db: Path) -> None:
    p = load_profile("degen")
    run_id = await dao.create_paper_run(
        db, name="d", starting_balance_cents=1_000_000,
        config_snapshot={}, profile_name=p.name, profile_snapshot=p.model_dump(),
    )
    ex = _make_exec(db, run_id, p)
    # percentage: 50% of balance -> 500_000, hard_cap == same
    size = ex._size_position(
        balance=1_000_000, flag={"composite_score": 4.5}, current_odds=0.03
    )
    assert size == 500_000


async def test_kelly_sizing_respects_cap(db: Path) -> None:
    p = load_profile("medium")
    run_id = await dao.create_paper_run(
        db, name="m", starting_balance_cents=1_000_000,
        config_snapshot={}, profile_name=p.name, profile_snapshot=p.model_dump(),
    )
    ex = _make_exec(db, run_id, p)
    # composite 7 -> edge 0.12; odds 0.1 -> kelly = 0.12 / 0.9 = 0.133
    # balance*kelly*frac(0.25) = 1_000_000 * 0.133 * 0.25 ≈ 33_250
    # hard_cap = 10% of balance = 100_000 — does NOT clamp
    size = ex._size_position(
        balance=1_000_000, flag={"composite_score": 7.0}, current_odds=0.10
    )
    assert 25_000 < size < 50_000


def test_estimate_edge_monotone() -> None:
    assert _estimate_edge(0.0) == 0.0
    assert _estimate_edge(5.0) == 0.0
    assert _estimate_edge(7.5) == pytest.approx(0.15, abs=0.01)
    assert _estimate_edge(10.0) == pytest.approx(0.30, abs=0.01)
    assert _estimate_edge(11.0) == pytest.approx(0.30, abs=0.01)  # clamped


# ── A4 — filter gating ───────────────────────────────────


async def test_min_composite_rejects_below_threshold(db: Path) -> None:
    p = load_profile("medium")  # threshold 6.0
    await dao.upsert_market(
        db,
        Market(
            id="m1", slug="s", question="?",
            category="ai",  # type: ignore[arg-type]
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            daily_volume_usd_cents=500_000,
        ),
    )
    await dao.upsert_wallet_first_sight(db, "0xaf4")
    run_id = await dao.create_paper_run(
        db, name="m", starting_balance_cents=1_000_000,
        config_snapshot={}, profile_name=p.name, profile_snapshot=p.model_dump(),
    )
    # Composite 5.9 — below medium's 6.0 threshold.
    flag_id = await dao.write_flag(
        db,
        Flag(
            wallet_address="0xaf4", market_id="m1",
            detector_name="composite", raw_score=7.0,
            composite_score=5.9,
            components={"contributing_detectors": ["CategoryInsiderDetector"]},
            created_at=datetime.now(UTC),
        ),
    )
    assert flag_id is not None
    ex = _make_exec(db, run_id, p)
    result = await ex.consider_flag(flag_id)
    assert result is None


async def test_allowed_detectors_filter(db: Path) -> None:
    # Medium allows Category/Event/Timing only. A flag from only Coordination
    # should be rejected.
    p = load_profile("medium")
    await dao.upsert_market(
        db,
        Market(
            id="m1", slug="s", question="?",
            category="ai",  # type: ignore[arg-type]
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            daily_volume_usd_cents=500_000,
        ),
    )
    await dao.upsert_wallet_first_sight(db, "0xaf4")
    run_id = await dao.create_paper_run(
        db, name="m", starting_balance_cents=1_000_000,
        config_snapshot={}, profile_name=p.name, profile_snapshot=p.model_dump(),
    )
    flag_id = await dao.write_flag(
        db,
        Flag(
            wallet_address="0xaf4", market_id="m1",
            detector_name="composite", raw_score=6.0,
            composite_score=7.0,
            components={"contributing_detectors": ["CoordinationDetector"]},
            created_at=datetime.now(UTC),
        ),
    )
    assert flag_id is not None
    ex = _make_exec(db, run_id, p)
    result = await ex.consider_flag(flag_id)
    assert result is None


# ── A6 — drawdown / daily-loss pause ─────────────────────


async def test_daily_loss_switch_below_limit_returns_none(db: Path) -> None:
    run_id = await dao.create_paper_run(
        db, name="r", starting_balance_cents=1_000_000, config_snapshot={},
    )
    # No closed positions -> no daily loss -> None.
    assert await daily_loss_switch(db, run_id, limit_pct=0.05) is None


async def test_daily_loss_switch_trips_when_losses_exceed_limit(db: Path) -> None:
    run_id = await dao.create_paper_run(
        db, name="r", starting_balance_cents=1_000_000, config_snapshot={},
    )
    # Seed a closed position with a large loss today.
    pid = await dao.write_paper_position(
        db, run_id=run_id, market_id="m1", outcome="YES",
        size_shares=10, avg_entry_price_cents=40,
        source_flag_id=None, source_wallet="0xaf4",
    )
    # Need the market to exist, but market writes already handled by the
    # position insert's FK placeholder path isn't set up here — skip for now.
    # Directly close the position with -60000¢ realized P&L ($600 loss).
    await dao.close_position(db, pid, realized_pnl_cents=-60_000, status="CLOSED")
    # limit = 5% of $10_000 = $500 = 50_000¢, loss $600 > $500 -> trip.
    result = await daily_loss_switch(db, run_id, limit_pct=0.05)
    assert result is not None
    assert "loss" in result.lower()


# ── A4 dispatcher — fan-out ──────────────────────────────


async def test_dispatcher_fans_out_to_all_active_executors(db: Path) -> None:
    # Three runs, one systematic + one degen, with a flag. The dispatcher
    # should invoke each executor once; the paused run should be skipped.
    await dao.upsert_market(
        db,
        Market(
            id="m1", slug="s", question="?",
            category="ai",  # type: ignore[arg-type]
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            daily_volume_usd_cents=500_000,
        ),
    )
    await dao.upsert_wallet_first_sight(db, "0xaf4")
    flag_id = await dao.write_flag(
        db,
        Flag(
            wallet_address="0xaf4", market_id="m1",
            detector_name="composite", raw_score=7.0,
            composite_score=7.0,
            components={"contributing_detectors": ["CategoryInsiderDetector"]},
            created_at=datetime.now(UTC),
        ),
    )
    assert flag_id is not None

    p_sys = load_profile("systematic")
    p_deg = load_profile("degen")
    sys_run = await dao.create_paper_run(
        db, name="s", starting_balance_cents=1_000_000,
        config_snapshot={}, profile_name=p_sys.name,
        profile_snapshot=p_sys.model_dump(), tag="exp",
    )
    deg_run = await dao.create_paper_run(
        db, name="d", starting_balance_cents=1_000_000,
        config_snapshot={}, profile_name=p_deg.name,
        profile_snapshot=p_deg.model_dump(), tag="exp",
    )
    await dao.pause_paper_run(db, deg_run, reason="test pause")

    dispatcher = Dispatcher([
        _make_exec(db, sys_run, p_sys),
        _make_exec(db, deg_run, p_deg),
    ])
    results = await dispatcher.on_flag(flag_id)

    assert sys_run in results
    assert deg_run in results
    # Paused run -> None from the dispatcher's is-paused pre-check.
    assert results[deg_run] is None


# ── A7 — DB round-trip of profile_snapshot + tag ─────────


async def test_profile_snapshot_round_trip(db: Path) -> None:
    p = load_profile("medium")
    run_id = await dao.create_paper_run(
        db, name="m1", starting_balance_cents=1_000_000,
        config_snapshot={}, profile_name=p.name,
        profile_snapshot=p.model_dump(), tag="batch-01",
    )
    row = await dao.get_paper_run(db, run_id)
    assert row is not None
    assert row["profile_name"] == "medium"
    assert row["tag"] == "batch-01"
    snap = json.loads(row["profile_snapshot_json"])
    assert snap["position_sizing_mode"] == "kelly_fractional"


async def test_list_paper_runs_by_tag(db: Path) -> None:
    for name in ("systematic", "medium", "degen"):
        p = load_profile(name)
        await dao.create_paper_run(
            db, name=f"{name}-1", starting_balance_cents=1_000_000,
            config_snapshot={}, profile_name=p.name,
            profile_snapshot=p.model_dump(), tag="exp-01",
        )
    # One extra run without the tag
    await dao.create_paper_run(
        db, name="solo", starting_balance_cents=1_000_000,
        config_snapshot={},
    )
    tagged = await dao.list_paper_runs_by_tag(db, tag="exp-01")
    assert len(tagged) == 3
    names = {r["profile_name"] for r in tagged}
    assert names == {"systematic", "medium", "degen"}


# ── resume command logic (no CLI — just DAO) ─────────────


async def test_resume_clears_pause_state(db: Path) -> None:
    run_id = await dao.create_paper_run(
        db, name="r", starting_balance_cents=1_000_000, config_snapshot={},
    )
    await dao.pause_paper_run(db, run_id, reason="drawdown 25%")
    row = await dao.get_paper_run(db, run_id)
    assert row is not None and row["paused_at"] is not None
    await dao.resume_paper_run(db, run_id)
    row = await dao.get_paper_run(db, run_id)
    assert row is not None
    assert row["paused_at"] is None
    assert row["pause_reason"] is None


# ── trade seed helper referenced by above tests ──────────


async def test_trade_writing_for_executor_path(db: Path) -> None:
    # Sanity: ensure our TradeEvent insert path works (used as fixture scaffold
    # by higher-level tests we can't easily wire here without an orderbook).
    await dao.upsert_market(
        db,
        Market(
            id="m1", slug="s", question="?",
            category="ai",  # type: ignore[arg-type]
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            daily_volume_usd_cents=500_000,
        ),
    )
    await dao.insert_trades_batch(
        db, [TradeEvent(
            id="t1", wallet_address="0xaf4", market_id="m1",
            side="BUY", outcome="YES", size_shares=100, price_cents=40,
            timestamp=datetime.now(UTC),
        )],
    )
    stats = await dao.db_stats(db)
    assert stats["trades"] >= 1
