"""Portfolio tests — build plan §4.3."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from polysim.config import BankrollConfig
from polysim.db import dao
from polysim.db.migrations.runner import apply_migrations
from polysim.models import Market
from polysim.paper.portfolio import Portfolio


@pytest.fixture
async def db(tmp_path: Path) -> Path:
    path = tmp_path / "t.db"
    await apply_migrations(path)
    return path


async def _seed_run(db: Path, *, balance: int = 1_000_000) -> int:
    return await dao.create_paper_run(
        db, name="test", starting_balance_cents=balance, config_snapshot={}
    )


async def _seed_market(db: Path, id_: str = "m1") -> None:
    await dao.upsert_market(db, Market(
        id=id_, slug=id_, question="Q?",
        created_at=datetime(2026, 4, 10, tzinfo=UTC),
        resolves_at=datetime(2026, 9, 30, tzinfo=UTC),
        daily_volume_usd_cents=47_200_00,
    ))


async def _open_position(
    db: Path,
    run_id: int,
    *,
    market_id: str = "m1",
    size: int = 100,
    entry: int = 40,
    source_wallet: str = "0xaf4",
    outcome: str = "YES",
) -> int:
    await _seed_market(db, market_id)
    return await dao.write_paper_position(
        db, run_id=run_id, market_id=market_id, outcome=outcome,
        size_shares=size, avg_entry_price_cents=entry,
        source_flag_id=None, source_wallet=source_wallet,
    )


class TestCaps:
    async def test_allows_first_position(self, db: Path) -> None:
        run_id = await _seed_run(db)
        p = Portfolio(db, run_id=run_id, bankroll=BankrollConfig())
        await _seed_market(db)
        result = await p.can_open(
            market_id="m1",
            source_wallet="0xaf4",
            intended_notional_cents=1_000,
        )
        assert result.allowed

    async def test_rejects_second_position_same_market(self, db: Path) -> None:
        run_id = await _seed_run(db)
        await _open_position(db, run_id, market_id="m1")
        p = Portfolio(db, run_id=run_id, bankroll=BankrollConfig())
        result = await p.can_open(
            market_id="m1", source_wallet="0xaf4", intended_notional_cents=100,
        )
        assert not result.allowed
        assert "existing OPEN" in result.reason

    async def test_max_open_positions(self, db: Path) -> None:
        run_id = await _seed_run(db)
        cfg = BankrollConfig(max_open_positions=2)
        for i in range(2):
            await _open_position(db, run_id, market_id=f"m{i}")
        p = Portfolio(db, run_id=run_id, bankroll=cfg)
        await _seed_market(db, "m_new")
        result = await p.can_open(
            market_id="m_new", source_wallet="0xaf4",
            intended_notional_cents=100,
        )
        assert not result.allowed
        assert "max_open_positions" in result.reason

    async def test_per_position_cap(self, db: Path) -> None:
        run_id = await _seed_run(db, balance=1_000_000)
        cfg = BankrollConfig(max_pct_per_position=0.02)
        await _seed_market(db)
        p = Portfolio(db, run_id=run_id, bankroll=cfg)
        # 2% of $10,000 = $200 = 20,000 cents. Try 30,000.
        result = await p.can_open(
            market_id="m1", source_wallet="0xaf4",
            intended_notional_cents=30_000,
        )
        assert not result.allowed
        assert "max_pct_per_position" in result.reason

    async def test_per_wallet_cap(self, db: Path) -> None:
        run_id = await _seed_run(db, balance=1_000_000)
        cfg = BankrollConfig(max_pct_per_source_wallet=0.10)
        # Open a position that uses 9% (90k cents) from wallet W.
        await _open_position(db, run_id, market_id="m1", size=900, entry=100,
                             source_wallet="0xW")
        p = Portfolio(db, run_id=run_id, bankroll=cfg)
        await _seed_market(db, "m2")
        # Try to add 20k more from W -> 110k > 100k cap -> rejected.
        result = await p.can_open(
            market_id="m2", source_wallet="0xW",
            intended_notional_cents=20_000,
        )
        assert not result.allowed
        assert "max_pct_per_source_wallet" in result.reason

    async def test_paused_run_rejects(self, db: Path) -> None:
        run_id = await _seed_run(db)
        await dao.pause_paper_run(db, run_id, reason="test")
        await _seed_market(db)
        p = Portfolio(db, run_id=run_id, bankroll=BankrollConfig())
        result = await p.can_open(
            market_id="m1", source_wallet="0xaf4",
            intended_notional_cents=100,
        )
        assert not result.allowed
        assert "paused" in result.reason


class TestClosePosition:
    async def test_win_pays_out_100_per_share(self, db: Path) -> None:
        run_id = await _seed_run(db, balance=1_000_000)
        # Spend 4,000 cents on 100 shares @ 40¢
        pos_id = await _open_position(db, run_id, size=100, entry=40)
        await dao.adjust_run_balance(db, run_id, -100 * 40)
        p = Portfolio(db, run_id=run_id, bankroll=BankrollConfig())

        realized = await p.close_position_at_payout(pos_id, payout_per_share_cents=100)
        # P&L = 100 * (100 - 40) = 6000
        assert realized == 6000
        balance = await p.current_balance_cents()
        # Started 1M, spent 4000, refunded 10000 -> 1M + 6000
        assert balance == 1_000_000 + 6_000

    async def test_loss_pays_zero(self, db: Path) -> None:
        run_id = await _seed_run(db, balance=1_000_000)
        pos_id = await _open_position(db, run_id, size=100, entry=40)
        await dao.adjust_run_balance(db, run_id, -100 * 40)
        p = Portfolio(db, run_id=run_id, bankroll=BankrollConfig())

        realized = await p.close_position_at_payout(pos_id, payout_per_share_cents=0)
        assert realized == -4_000
        balance = await p.current_balance_cents()
        assert balance == 1_000_000 - 4_000

    async def test_invalid_refunds_entry_cost(self, db: Path) -> None:
        """Spec §9: invalid -> realized_pnl = 0, refund entry cost."""
        run_id = await _seed_run(db, balance=1_000_000)
        pos_id = await _open_position(db, run_id, size=100, entry=40)
        await dao.adjust_run_balance(db, run_id, -100 * 40)
        p = Portfolio(db, run_id=run_id, bankroll=BankrollConfig())

        realized = await p.close_position_at_payout(
            pos_id, payout_per_share_cents=0, invalid=True,
        )
        assert realized == 0
        balance = await p.current_balance_cents()
        # Refunds 4000 (entry cost) -> back to starting balance.
        assert balance == 1_000_000


class TestExposure:
    async def test_exposure_sums_across_open(self, db: Path) -> None:
        run_id = await _seed_run(db)
        await _open_position(db, run_id, market_id="m1", size=100, entry=40,
                             source_wallet="0xW")
        await _open_position(db, run_id, market_id="m2", size=50, entry=60,
                             source_wallet="0xW")
        await _open_position(db, run_id, market_id="m3", size=10, entry=20,
                             source_wallet="0xOther")
        p = Portfolio(db, run_id=run_id, bankroll=BankrollConfig())
        # W: 100*40 + 50*60 = 4000 + 3000 = 7000
        assert await p.exposure_by_wallet("0xW") == 7_000
        # Other: 200
        assert await p.exposure_by_wallet("0xOther") == 200
        # m1 exposure = 4000
        assert await p.exposure_by_market("m1") == 4_000
