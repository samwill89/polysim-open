"""Tournament scoring MTM tests.

The allocator retires/promotes on return_pct = (realized + unrealized) /
start. Before this fix unrealized was hardcoded 0, so variants holding
open positions were scored on realized-only cash moves (and deploying
capital even read as 'drawdown'). Now open positions mark at BID (§4.1)
when a book snapshot exists, and at entry (0 unrealized) when not.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import aiosqlite
import pytest

from polysim.db import dao
from polysim.db.migrations.runner import apply_migrations
from polysim.models import Market
from polysim.tournament.loop import TOURNAMENT_TAG, TournamentAllocatorLoop
from polysim.tournament.variants import LIVE_VARIANT_NAMES


@pytest.fixture
async def db(tmp_path: Path) -> Path:
    path = tmp_path / "t.db"
    await apply_migrations(path)
    return path


async def _tournament_run(
    db: Path,
    *,
    name: str,
    balance_cents: int,
    start_cents: int = 1_000_000,
) -> int:
    run_id = await dao.create_paper_run(
        db,
        name=f"tournament-{name}",
        starting_balance_cents=start_cents,
        config_snapshot={},
        profile_name="systematic",
        profile_snapshot={"name": name},
        tag=TOURNAMENT_TAG,
    )
    async with aiosqlite.connect(str(db)) as conn:
        await conn.execute(
            "UPDATE paper_runs SET current_balance_cents = ? WHERE id = ?",
            (balance_cents, run_id),
        )
        await conn.commit()
    return run_id


async def _open_position(
    db: Path,
    run_id: int,
    *,
    market_id: str,
    size: int,
    entry: int,
) -> None:
    await dao.upsert_market(
        db,
        Market(
            id=market_id,
            slug=market_id,
            question=f"{market_id}?",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        ),
    )
    await dao.write_paper_position(
        db,
        run_id=run_id,
        market_id=market_id,
        outcome="YES",
        size_shares=size,
        avg_entry_price_cents=entry,
        source_flag_id=None,
        source_wallet="0xabc",
    )


async def test_unrealized_marks_open_positions_at_bid(db: Path) -> None:
    # $10k start; spent 100 shares * 40c = $40 → cash 996_000.
    run_id = await _tournament_run(db, name="v1", balance_cents=996_000)
    await _open_position(db, run_id, market_id="m1", size=100, entry=40)
    # Book: bid 60 / ask 62 → unrealized = 100 * (60 - 40) = +2000.
    await dao.write_orderbook_snapshot(
        db,
        market_id="m1",
        outcome="YES",
        asks=[{"price_cents": 62, "size_shares": 500}],
        bids=[{"price_cents": 60, "size_shares": 500}],
    )
    loop = TournamentAllocatorLoop(db)
    scores = await loop._score_active_runs()
    assert len(scores) == 1
    s = scores[0]
    assert s.unrealized_pnl_cents == 2_000
    assert s.realized_pnl_cents == 0
    # Equity = 996_000 + 4_000 cost + 2_000 unreal > start → no drawdown.
    assert s.max_drawdown_pct == 0.0
    assert s.total_pnl_cents == 2_000


async def test_no_book_marks_at_entry_zero_unrealized(db: Path) -> None:
    run_id = await _tournament_run(db, name="v2", balance_cents=996_000)
    await _open_position(db, run_id, market_id="m2", size=100, entry=40)
    loop = TournamentAllocatorLoop(db)
    scores = await loop._score_active_runs()
    s = scores[0]
    assert s.unrealized_pnl_cents == 0
    # Deployed-but-unmarked capital is NOT drawdown: equity = cash + cost.
    assert s.max_drawdown_pct == 0.0


async def test_losing_marks_show_up_as_drawdown(db: Path) -> None:
    run_id = await _tournament_run(db, name="v3", balance_cents=900_000)
    await _open_position(db, run_id, market_id="m3", size=2_500, entry=40)
    # Bid collapsed to 10c → unrealized = 2500 * (10 - 40) = -75_000.
    await dao.write_orderbook_snapshot(
        db,
        market_id="m3",
        outcome="YES",
        asks=[{"price_cents": 12, "size_shares": 5000}],
        bids=[{"price_cents": 10, "size_shares": 5000}],
    )
    loop = TournamentAllocatorLoop(db)
    s = (await loop._score_active_runs())[0]
    assert s.unrealized_pnl_cents == -75_000
    # equity = 900k cash + 100k cost - 75k = 925k → 7.5% drawdown.
    assert s.max_drawdown_pct == pytest.approx(0.075)


async def test_variant_name_read_from_snapshot(db: Path) -> None:
    await _tournament_run(db, name="signal_sized", balance_cents=1_000_000)
    loop = TournamentAllocatorLoop(db)
    s = (await loop._score_active_runs())[0]
    assert s.variant_name == "signal_sized"


async def test_bid_cache_shared_across_runs(db: Path) -> None:
    # Two runs holding the same market — the mark must be consistent.
    r1 = await _tournament_run(db, name="a", balance_cents=996_000)
    r2 = await _tournament_run(db, name="b", balance_cents=996_000)
    await _open_position(db, r1, market_id="m4", size=100, entry=40)
    await _open_position(db, r2, market_id="m4", size=100, entry=40)
    await dao.write_orderbook_snapshot(
        db,
        market_id="m4",
        outcome="YES",
        asks=[{"price_cents": 52, "size_shares": 500}],
        bids=[{"price_cents": 50, "size_shares": 500}],
    )
    loop = TournamentAllocatorLoop(db)
    scores = await loop._score_active_runs()
    assert [s.unrealized_pnl_cents for s in scores] == [1_000, 1_000]


async def test_seed_pool_pauses_retired_and_opens_only_live_variants(
    db: Path,
) -> None:
    retired_id = await _tournament_run(db, name="wide_stop", balance_cents=1_000_000)
    await dao.pause_paper_run(db, retired_id, reason="old allocator pause")
    loop = TournamentAllocatorLoop(db)
    assert await loop.seed_pool() == len(LIVE_VARIANT_NAMES)

    retired = await dao.get_paper_run(db, retired_id)
    assert retired is not None
    assert retired["paused_at"] is not None
    assert "strategy_set_v3" in str(retired["pause_reason"])

    rows = await dao.list_paper_runs_by_tag(db, tag=TOURNAMENT_TAG)
    active_names = {
        str(row["name"]).removeprefix("tournament-") for row in rows if row["paused_at"] is None
    }
    assert active_names == set(LIVE_VARIANT_NAMES)
