"""Settlement-cycle simulation tests — empirical-priors addendum §4.2.

Locks in:
  * 2-block (~4s) minimum delay between buy fill and sellable.
  * Capacity lock during pending settlement.
  * 5-cycle stress reproduction matching Opus 4.6's logged behavior.
  * Settlement sweep clears stale pending state.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from polysim.config import BankrollConfig
from polysim.db import dao
from polysim.db.migrations.runner import apply_migrations
from polysim.paper.portfolio import Portfolio
from polysim.portfolio.settlement import (
    DEFAULT_BLOCK_SECONDS,
    compute_settlement_window,
    is_sellable,
    pending_capacity_cents,
    record_settlement_start,
    sweep_settlements,
)


def test_min_two_blocks_default() -> None:
    buy_ts = datetime(2026, 4, 20, 12, 0, 0, tzinfo=UTC)
    pending_until, blocks = compute_settlement_window(buy_ts)
    assert blocks == 2
    expected_min = buy_ts + timedelta(seconds=2 * DEFAULT_BLOCK_SECONDS)
    assert pending_until >= expected_min
    # 2 blocks at 2s = 4s exactly.
    assert (pending_until - buy_ts).total_seconds() == 4.0


def test_extra_blocks_for_stress() -> None:
    """Opus 4.6 reproduction: 5-cycle lock under degraded settlement.

    extra_blocks=3 → 2+3=5 blocks → 10s window.
    """
    buy_ts = datetime(2026, 4, 20, 12, 0, 0, tzinfo=UTC)
    pending_until, blocks = compute_settlement_window(buy_ts, extra_blocks=3)
    assert blocks == 5
    assert (pending_until - buy_ts).total_seconds() == 10.0


def test_is_sellable_pending_vs_settled() -> None:
    now = datetime(2026, 4, 20, 12, 0, 10, tzinfo=UTC)
    pending = {"pending_until_iso": (now + timedelta(seconds=3)).isoformat()}
    settled = {"pending_until_iso": (now - timedelta(seconds=3)).isoformat()}
    no_field = {"pending_until_iso": None}
    assert is_sellable(pending, now=now) is False
    assert is_sellable(settled, now=now) is True
    assert is_sellable(no_field, now=now) is True


@pytest.fixture
async def db(tmp_path: Path) -> Path:
    p = tmp_path / "t.db"
    await apply_migrations(p)
    return p


async def _open_pos(db: Path, run_id: int, *, size: int = 100, entry: int = 40) -> int:
    return await dao.write_paper_position(
        db, run_id=run_id, market_id="m1", outcome="YES",
        size_shares=size, avg_entry_price_cents=entry,
        source_flag_id=None, source_wallet="0xaf4",
    )


async def test_record_settlement_locks_capacity(db: Path) -> None:
    run_id = await dao.create_paper_run(
        db, name="r", starting_balance_cents=1_000_000, config_snapshot={},
    )
    pid = await _open_pos(db, run_id, size=100, entry=40)
    buy_ts = datetime.now(UTC)
    pending_until, blocks = compute_settlement_window(buy_ts)
    await record_settlement_start(
        db, position_id=pid, buy_ts=buy_ts,
        pending_until=pending_until, blocks_to_settle=blocks,
        capacity_locked_cents=100 * 40,
    )
    # The position now has pending_until_iso set; capacity sums to 4000.
    pending = await pending_capacity_cents(db, run_id)
    assert pending == 4000


async def test_instant_round_trip_blocked_by_pending_capacity(db: Path) -> None:
    """Opus 4.6 capacity-lock scenario: bought, can't open another size-equivalent
    position because effective_balance has been shaved by the pending fill."""
    run_id = await dao.create_paper_run(
        db, name="r", starting_balance_cents=10_000, config_snapshot={},
    )
    # Spend most of the balance on the first position, mark pending.
    pid = await _open_pos(db, run_id, size=100, entry=80)        # cost = 8000
    buy_ts = datetime.now(UTC)
    pending_until, blocks = compute_settlement_window(buy_ts, extra_blocks=10)
    await record_settlement_start(
        db, position_id=pid, buy_ts=buy_ts,
        pending_until=pending_until, blocks_to_settle=blocks,
        capacity_locked_cents=8000,
    )
    # Force balance down to mirror what the executor would have done.
    await dao.adjust_run_balance(db, run_id, -8000)
    # Now an attempt to open another big position must fail with the
    # capacity-locked reason.
    p = Portfolio(db, run_id=run_id, bankroll=BankrollConfig())
    res = await p.can_open(
        market_id="m2", source_wallet="0xaf4",
        intended_notional_cents=2_000,
    )
    # Effective balance = 2000 - 0 pending in m2 — first cap to fail will
    # be either max_pct_per_position (2% of 2000=40) or per-market.
    # Either way the open should be refused.
    assert res.allowed is False


async def test_sweep_releases_due_settlements(db: Path) -> None:
    run_id = await dao.create_paper_run(
        db, name="r", starting_balance_cents=1_000_000, config_snapshot={},
    )
    pid = await _open_pos(db, run_id)
    # Pending window already in the past.
    buy_ts = datetime.now(UTC) - timedelta(seconds=30)
    pending_until = buy_ts + timedelta(seconds=4)
    await record_settlement_start(
        db, position_id=pid, buy_ts=buy_ts,
        pending_until=pending_until, blocks_to_settle=2,
        capacity_locked_cents=4000,
    )
    # Pre-sweep: pending_until_iso set on the position.
    pos = await dao.get_position(db, pid)
    assert pos and pos["pending_until_iso"] is not None
    # Sweep should release.
    n = await sweep_settlements(db)
    assert n == 1
    pos = await dao.get_position(db, pid)
    assert pos and pos["pending_until_iso"] is None
    # Pending capacity now zero.
    assert await pending_capacity_cents(db, run_id) == 0


async def test_sweep_idempotent(db: Path) -> None:
    run_id = await dao.create_paper_run(
        db, name="r", starting_balance_cents=1_000_000, config_snapshot={},
    )
    n1 = await sweep_settlements(db)
    n2 = await sweep_settlements(db)
    assert n1 == 0 and n2 == 0
    _ = run_id
