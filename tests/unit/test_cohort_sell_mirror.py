"""Test the SELL-mirror path in CopyOnCohortLoop.

When a cohort wallet sells, _mirror_sell_close should find any open
paper position sourced from that wallet on the same (market, outcome),
credit the proceeds, record a SELL fill, and close it at the wallet's price.
"""

from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest

from polysim.live import _mirror_sell_close


async def _build_db(path: Path) -> int:
    """Tiny schema for mirror-close accounting. Returns position id."""
    async with aiosqlite.connect(str(path)) as db:
        await db.executescript(
            """
            CREATE TABLE markets (
                id TEXT PRIMARY KEY,
                slug TEXT NOT NULL,
                question TEXT NOT NULL,
                category TEXT NOT NULL,
                created_at TEXT NOT NULL,
                resolves_at TEXT,
                resolved_outcome TEXT,
                resolved_at TEXT,
                daily_volume_usd_cents INTEGER,
                metadata_json TEXT
            );
            CREATE TABLE paper_runs (
                id INTEGER PRIMARY KEY,
                current_balance_cents INTEGER NOT NULL
            );
            CREATE TABLE paper_positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                market_id TEXT NOT NULL,
                outcome TEXT NOT NULL,
                size_shares INTEGER NOT NULL,
                avg_entry_price_cents INTEGER NOT NULL,
                opened_at TEXT NOT NULL,
                closed_at TEXT,
                realized_pnl_cents INTEGER,
                source_flag_id INTEGER,
                source_wallet TEXT,
                status TEXT NOT NULL,
                pending_until_iso TEXT
            );
            CREATE TABLE paper_fills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                position_id INTEGER NOT NULL,
                side TEXT NOT NULL,
                size_shares INTEGER NOT NULL,
                fill_price_cents INTEGER NOT NULL,
                intended_price_cents INTEGER NOT NULL,
                slippage_cents INTEGER NOT NULL,
                latency_ms INTEGER NOT NULL,
                fee_cents INTEGER NOT NULL,
                timestamp TEXT NOT NULL
            );
            """
        )
        await db.execute(
            "INSERT INTO markets(id, slug, question, category, created_at, metadata_json) "
            "VALUES ('mkt_1', 'mkt-1', 'Test market?', 'politics', "
            "'2026-04-28T00:00:00+00:00', '{}')"
        )
        # $10,000 starting cash less the $40 entry cost already paid.
        await db.execute(
            "INSERT INTO paper_runs(id, current_balance_cents) VALUES (1, 996000)"
        )
        cur = await db.execute(
            """
            INSERT INTO paper_positions(
                run_id, market_id, outcome, size_shares,
                avg_entry_price_cents, opened_at, source_wallet, status
            ) VALUES (1, 'mkt_1', 'YES', 100, 40,
                      '2026-04-28T00:00:00+00:00', '0xcohort', 'OPEN')
            """,
        )
        new_id = int(cur.lastrowid or 0)
        await cur.close()
        await db.commit()
        return new_id


@pytest.mark.asyncio
async def test_mirror_sell_closes_matching_position(tmp_path: Path) -> None:
    db = tmp_path / "test.db"
    pos_id = await _build_db(db)
    # Cohort wallet sells YES at 60c → we exit at 60c. Bought at 40, +20c x 100 = +$20.
    closed = await _mirror_sell_close(
        db,
        {
            "wallet_address": "0xcohort",
            "market_id": "mkt_1",
            "outcome": "YES",
            "side": "SELL",
            "size_shares": 100,
            "price_cents": 60,
            "id": "tx_abc",
        },
    )
    assert closed == 1
    async with aiosqlite.connect(str(db)) as conn, conn.execute(
        "SELECT status, realized_pnl_cents FROM paper_positions WHERE id = ?",
        (pos_id,),
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    assert row[0] == "CLOSED"
    assert row[1] == (60 - 40) * 100   # +2,000c = +$20.00
    async with aiosqlite.connect(str(db)) as conn:
        balance = await conn.execute_fetchall(
            "SELECT current_balance_cents FROM paper_runs WHERE id = 1"
        )
        fills = await conn.execute_fetchall(
            "SELECT side, fill_price_cents, size_shares FROM paper_fills"
        )
    assert balance == [(1_002_000,)]
    assert fills == [("SELL", 60, 100)]


@pytest.mark.asyncio
async def test_mirror_sell_only_closes_matching_market_outcome(
    tmp_path: Path,
) -> None:
    """A position on a different market or outcome must NOT be closed."""
    db = tmp_path / "test.db"
    await _build_db(db)
    closed_wrong_market = await _mirror_sell_close(
        db,
        {
            "wallet_address": "0xcohort",
            "market_id": "mkt_OTHER",
            "outcome": "YES",
            "side": "SELL",
            "size_shares": 100,
            "price_cents": 60,
            "id": "x",
        },
    )
    assert closed_wrong_market == 0
    closed_wrong_outcome = await _mirror_sell_close(
        db,
        {
            "wallet_address": "0xcohort",
            "market_id": "mkt_1",
            "outcome": "NO",
            "side": "SELL",
            "size_shares": 100,
            "price_cents": 60,
            "id": "y",
        },
    )
    assert closed_wrong_outcome == 0


@pytest.mark.asyncio
async def test_mirror_sell_only_closes_matching_wallet(tmp_path: Path) -> None:
    """A SELL from a different cohort wallet must NOT close OUR position
    that came from a specific wallet — selling is per-wallet attribution."""
    db = tmp_path / "test.db"
    await _build_db(db)
    closed = await _mirror_sell_close(
        db,
        {
            "wallet_address": "0xother_wallet",
            "market_id": "mkt_1",
            "outcome": "YES",
            "side": "SELL",
            "size_shares": 100,
            "price_cents": 60,
            "id": "z",
        },
    )
    assert closed == 0


@pytest.mark.asyncio
async def test_mirror_sell_at_low_price_books_loss(tmp_path: Path) -> None:
    """If cohort sells below entry, we book a loss."""
    db = tmp_path / "test.db"
    await _build_db(db)
    closed = await _mirror_sell_close(
        db,
        {
            "wallet_address": "0xcohort",
            "market_id": "mkt_1",
            "outcome": "YES",
            "side": "SELL",
            "size_shares": 100,
            "price_cents": 25,   # entry was 40c → -$15 realized
            "id": "loss",
        },
    )
    assert closed == 1
    async with aiosqlite.connect(str(db)) as conn, conn.execute(
        "SELECT realized_pnl_cents FROM paper_positions WHERE source_wallet = ?",
        ("0xcohort",),
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    assert row[0] == (25 - 40) * 100   # -1,500c = -$15


@pytest.mark.asyncio
async def test_mirror_sell_cannot_double_credit(tmp_path: Path) -> None:
    db = tmp_path / "test.db"
    await _build_db(db)
    trade = {
        "wallet_address": "0xcohort",
        "market_id": "mkt_1",
        "outcome": "YES",
        "side": "SELL",
        "size_shares": 100,
        "price_cents": 60,
        "id": "same-sell",
    }
    assert await _mirror_sell_close(db, trade) == 1
    assert await _mirror_sell_close(db, trade) == 0
    async with aiosqlite.connect(str(db)) as conn:
        balance = await conn.execute_fetchall(
            "SELECT current_balance_cents FROM paper_runs WHERE id = 1"
        )
        fills = await conn.execute_fetchall("SELECT COUNT(*) FROM paper_fills")
    assert balance == [(1_002_000,)]
    assert fills == [(1,)]


@pytest.mark.asyncio
async def test_mirror_sell_deducts_persisted_market_fee(tmp_path: Path) -> None:
    db = tmp_path / "test.db"
    pos_id = await _build_db(db)
    async with aiosqlite.connect(str(db)) as conn:
        await conn.execute(
            "UPDATE markets SET metadata_json = ? WHERE id = 'mkt_1'",
            ('{"feesEnabled": true, "feeSchedule": {"rate": 0.04, "exponent": 1}}',),
        )
        await conn.commit()
    assert (
        await _mirror_sell_close(
            db,
            {
                "wallet_address": "0xcohort",
                "market_id": "mkt_1",
                "outcome": "YES",
                "side": "SELL",
                "size_shares": 100,
                "price_cents": 60,
                "id": "fee-exit",
            },
        )
        == 1
    )
    async with aiosqlite.connect(str(db)) as conn:
        position = await conn.execute_fetchall(
            "SELECT realized_pnl_cents FROM paper_positions WHERE id = ?",
            (pos_id,),
        )
        balance = await conn.execute_fetchall(
            "SELECT current_balance_cents FROM paper_runs WHERE id = 1"
        )
        fill = await conn.execute_fetchall("SELECT fee_cents FROM paper_fills")
    assert position == [(1_904,)]
    assert balance == [(1_001_904,)]
    assert fill == [(96,)]
