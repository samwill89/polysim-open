"""Historical reconciliation for mirror exits that lost their cash credit."""

from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest

from polysim.db.migrations.runner import apply_migrations


@pytest.mark.asyncio
async def test_migration_reconciles_once_and_records_evidence(tmp_path: Path) -> None:
    db_path = tmp_path / "reconcile.db"
    await apply_migrations(db_path)

    async with aiosqlite.connect(str(db_path)) as db:
        await db.execute(
            "INSERT INTO markets(id, slug, question, created_at) "
            "VALUES ('market', 'market', 'Question?', "
            "'2026-07-01T00:00:00+00:00')"
        )
        await db.execute(
            "INSERT INTO wallets(address, first_seen_at) "
            "VALUES ('0xwallet', '2026-07-01T00:00:00+00:00')"
        )
        await db.execute(
            "INSERT INTO trades(id, wallet_address, market_id, side, outcome, "
            "size_shares, price_cents, timestamp) VALUES "
            "('buy', '0xwallet', 'market', 'BUY', 'YES', 100, 40, "
            "'2026-07-01T00:00:00+00:00'), "
            "('sell', '0xwallet', 'market', 'SELL', 'YES', 100, 60, "
            "'2026-07-02T00:00:00+00:00')"
        )
        flag = await db.execute(
            "INSERT INTO flags(wallet_address, market_id, trade_id, "
            "detector_name, raw_score, composite_score, components_json, "
            "created_at) VALUES ('0xwallet', 'market', 'buy', 'CohortCopy', "
            "10, 10, '{}', '2026-07-01T00:00:00+00:00')"
        )
        flag_id = int(flag.lastrowid or 0)
        await flag.close()
        run = await db.execute(
            "INSERT INTO paper_runs("
            "name, started_at, config_json, starting_balance_cents, "
            "current_balance_cents, notes, tag) "
            "VALUES ('affected', '2026-07-01T00:00:00+00:00', '{}', "
            "1000000, 996000, '', 'tournament_v1')"
        )
        run_id = int(run.lastrowid or 0)
        await run.close()
        pos = await db.execute(
            "INSERT INTO paper_positions("
            "run_id, market_id, outcome, size_shares, avg_entry_price_cents, "
            "opened_at, closed_at, realized_pnl_cents, source_flag_id, "
            "source_wallet, status) "
            "VALUES (?, 'market', 'YES', 100, 40, "
            "'2026-07-01T00:00:00+00:00', '2026-07-02T00:00:00+00:00', "
            "2000, ?, '0xwallet', 'CLOSED')",
            (run_id, flag_id),
        )
        position_id = int(pos.lastrowid or 0)
        await pos.close()
        unaffected = await db.execute(
            "INSERT INTO paper_positions("
            "run_id, market_id, outcome, size_shares, avg_entry_price_cents, "
            "opened_at, closed_at, realized_pnl_cents, source_flag_id, "
            "source_wallet, status) "
            "VALUES (?, 'market', 'YES', 50, 40, "
            "'2026-07-01T00:00:00+00:00', '2026-07-02T00:00:00+00:00', "
            "0, ?, '0xwallet', 'CLOSED')",
            (run_id, flag_id),
        )
        unaffected_position_id = int(unaffected.lastrowid or 0)
        await unaffected.close()
        # Re-run only the reconciliation migration with the affected fixture.
        await db.execute("DELETE FROM _migrations WHERE version = 10")
        await db.commit()

    assert await apply_migrations(db_path) == ["0010_reconcile_mirror_exits.sql"]
    async with aiosqlite.connect(str(db_path)) as db:
        balance = await db.execute_fetchall(
            "SELECT current_balance_cents FROM paper_runs WHERE id = ?",
            (run_id,),
        )
        fills = await db.execute_fetchall(
            "SELECT side, fill_price_cents, size_shares FROM paper_fills WHERE position_id = ?",
            (position_id,),
        )
        evidence = await db.execute_fetchall(
            "SELECT reason, credit_cents, credited_at IS NOT NULL "
            "FROM paper_balance_reconciliations WHERE position_id = ?",
            (position_id,),
        )
        unaffected_rows = await db.execute_fetchall(
            "SELECT COUNT(*) FROM paper_balance_reconciliations WHERE position_id = ?",
            (unaffected_position_id,),
        )
    assert balance == [(1_002_000,)]
    assert fills == [("SELL", 60, 100)]
    assert evidence == [("mirror_sell_missing_credit", 6_000, 1)]
    assert unaffected_rows == [(0,)]

    # Simulate migration bookkeeping loss. Durable evidence prevents a second
    # cash credit or duplicate fill when version 10 is retried.
    async with aiosqlite.connect(str(db_path)) as db:
        await db.execute("DELETE FROM _migrations WHERE version = 10")
        await db.commit()
    assert await apply_migrations(db_path) == ["0010_reconcile_mirror_exits.sql"]
    async with aiosqlite.connect(str(db_path)) as db:
        balance_again = await db.execute_fetchall(
            "SELECT current_balance_cents FROM paper_runs WHERE id = ?",
            (run_id,),
        )
        fill_count = await db.execute_fetchall(
            "SELECT COUNT(*) FROM paper_fills WHERE position_id = ?",
            (position_id,),
        )
    assert balance_again == [(1_002_000,)]
    assert fill_count == [(1,)]
