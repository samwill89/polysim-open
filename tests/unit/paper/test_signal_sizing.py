"""Signal-policy integration in ProfilePaperExecutor.consider_flag.

Proves the degradation contract end-to-end on a real temp DB:
  * policy 'off'            → identical to today (columns NULL)
  * policy 'size'           → bounded multiplier applied + recorded
  * missing signal          → exactly neutral (never blocks, never scales)
  * policy 'gate_and_size'  → confident dead conversation skips the copy;
                              absent signal fails open
"""

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
from polysim.signals.schema import MarketSignal
from polysim.signals.service import write_market_signal
from polysim.utils.time import now_utc


@pytest.fixture
async def db(tmp_path: Path) -> Path:
    path = tmp_path / "t.db"
    await apply_migrations(path)
    return path


def _profile(**overrides: object) -> RiskProfile:
    base = load_profile("systematic")
    return base.model_copy(update=overrides)


async def _seed_flag(db: Path, *, market_id: str = "m1") -> int:
    """Market + triggering trade + CohortCopy-style flag (composite 10)."""
    await dao.upsert_market(db, Market(
        id=market_id, slug=market_id, question=f"Will {market_id} resolve YES?",
        category="box_office",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        daily_volume_usd_cents=500_000,
    ))
    await dao.upsert_wallet_first_sight(db, "0xcafe")
    trade_id = f"t-{market_id}"
    await dao.insert_trades_batch(db, [TradeEvent(
        id=trade_id, wallet_address="0xcafe", market_id=market_id,
        side="BUY", outcome="YES", size_shares=500, price_cents=40,
        timestamp=now_utc(),
    )])
    flag_id = await dao.write_flag(db, Flag(
        wallet_address="0xcafe", market_id=market_id, trade_id=trade_id,
        detector_name="CohortCopy", raw_score=10.0, composite_score=10.0,
        components={"contributing_detectors": ["CohortCopy"]},
        created_at=now_utc(),
    ))
    assert flag_id is not None
    return flag_id


async def _run_with(db: Path, profile: RiskProfile) -> ProfilePaperExecutor:
    run_id = await dao.create_paper_run(
        db, name=f"run-{profile.name}-{id(profile)}",
        starting_balance_cents=1_000_000,
        config_snapshot={}, profile_name=profile.name,
        profile_snapshot=profile.model_dump(),
    )
    return ProfilePaperExecutor(
        db, run_id=run_id, profile=profile,
        bankroll=BankrollConfig(), fill_model=FillModel(),
    )


async def _write_signal(
    db: Path, market_id: str, *, composite: float, confidence: float,
) -> None:
    await write_market_signal(db, MarketSignal(
        market_id=market_id, ts=now_utc(),
        composite=composite, confidence=confidence,
    ))


async def _position(db: Path, pid: int) -> dict:
    import aiosqlite
    async with aiosqlite.connect(str(db)) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT * FROM paper_positions WHERE id = ?", (pid,),
        ) as cur:
            row = await cur.fetchone()
    assert row is not None
    return dict(row)


async def test_policy_off_leaves_columns_null(db: Path) -> None:
    flag_id = await _seed_flag(db, market_id="m-off")
    await _write_signal(db, "m-off", composite=0.9, confidence=0.9)
    ex = await _run_with(db, _profile())  # signal_policy defaults to 'off'
    pid = await ex.consider_flag(flag_id)
    assert pid is not None
    pos = await _position(db, pid)
    assert pos["signal_composite"] is None
    assert pos["signal_multiplier"] is None


async def test_policy_size_scales_up_on_high_conviction(db: Path) -> None:
    flag_id = await _seed_flag(db, market_id="m-up")
    await _write_signal(db, "m-up", composite=1.0, confidence=1.0)

    base_ex = await _run_with(db, _profile())
    sized_ex = await _run_with(db, _profile(signal_policy="size"))
    base_pid = await base_ex.consider_flag(flag_id)
    sized_pid = await sized_ex.consider_flag(flag_id)
    assert base_pid is not None and sized_pid is not None

    base_pos = await _position(db, base_pid)
    sized_pos = await _position(db, sized_pid)
    assert sized_pos["signal_multiplier"] == pytest.approx(1.5)
    assert sized_pos["signal_composite"] == pytest.approx(1.0)
    # 1.5x the notional target → ~1.5x the shares (integer rounding aside).
    ratio = sized_pos["size_shares"] / base_pos["size_shares"]
    assert 1.4 <= ratio <= 1.6


async def test_policy_size_scales_down_on_dead_conversation(db: Path) -> None:
    flag_id = await _seed_flag(db, market_id="m-down")
    await _write_signal(db, "m-down", composite=0.0, confidence=1.0)
    base_ex = await _run_with(db, _profile())
    sized_ex = await _run_with(db, _profile(signal_policy="size"))
    base_pid = await base_ex.consider_flag(flag_id)
    sized_pid = await sized_ex.consider_flag(flag_id)
    assert base_pid is not None and sized_pid is not None
    base_pos = await _position(db, base_pid)
    sized_pos = await _position(db, sized_pid)
    ratio = sized_pos["size_shares"] / base_pos["size_shares"]
    assert 0.4 <= ratio <= 0.6


async def test_policy_size_missing_signal_is_neutral(db: Path) -> None:
    flag_id = await _seed_flag(db, market_id="m-nosig")
    base_ex = await _run_with(db, _profile())
    sized_ex = await _run_with(db, _profile(signal_policy="size"))
    base_pid = await base_ex.consider_flag(flag_id)
    sized_pid = await sized_ex.consider_flag(flag_id)
    assert base_pid is not None and sized_pid is not None
    base_pos = await _position(db, base_pid)
    sized_pos = await _position(db, sized_pid)
    assert sized_pos["size_shares"] == base_pos["size_shares"]
    assert sized_pos["signal_multiplier"] is None


async def test_gate_blocks_confident_dead_conversation(db: Path) -> None:
    flag_id = await _seed_flag(db, market_id="m-gate")
    await _write_signal(db, "m-gate", composite=0.05, confidence=0.9)
    gated_ex = await _run_with(db, _profile(signal_policy="gate_and_size"))
    assert await gated_ex.consider_flag(flag_id) is None


async def test_gate_fails_open_without_signal(db: Path) -> None:
    flag_id = await _seed_flag(db, market_id="m-gate-open")
    gated_ex = await _run_with(db, _profile(signal_policy="gate_and_size"))
    pid = await gated_ex.consider_flag(flag_id)
    assert pid is not None


async def test_gate_fails_open_on_low_confidence(db: Path) -> None:
    flag_id = await _seed_flag(db, market_id="m-gate-lowconf")
    await _write_signal(db, "m-gate-lowconf", composite=0.05, confidence=0.2)
    gated_ex = await _run_with(db, _profile(signal_policy="gate_and_size"))
    pid = await gated_ex.consider_flag(flag_id)
    assert pid is not None
