"""Migration runner tests. Phase 0."""

from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest

from polysim.db.migrations.runner import apply_migrations, current_version


@pytest.mark.asyncio
async def test_applies_from_empty(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    applied = await apply_migrations(db)
    assert applied, "expected at least 0001_init to apply"
    assert await current_version(db) >= 1


@pytest.mark.asyncio
async def test_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    await apply_migrations(db)
    second = await apply_migrations(db)
    assert second == [], "second invocation should apply no new migrations"


@pytest.mark.asyncio
async def test_schema_has_expected_tables(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    await apply_migrations(db)
    expected = {
        "markets",
        "wallets",
        "trades",
        "wallet_profiles",
        "flags",
        "flag_costs",
        "clock_skew_samples",
        "known_insiders",
        "paper_runs",
        "paper_positions",
        "paper_fills",
        "paper_balance_reconciliations",
        "market_evidence_assessments",
        "trade_risk_decisions",
        "metrics_snapshots",
        "meta",
        "_migrations",
    }
    async with aiosqlite.connect(str(db)) as conn:
        cur = await conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        actual = {row[0] for row in await cur.fetchall()}
    missing = expected - actual
    assert not missing, f"missing tables: {missing}"


@pytest.mark.asyncio
async def test_v3_retirement_covers_every_legacy_experiment_lane(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    await apply_migrations(db)
    async with aiosqlite.connect(str(db)) as conn:
        for name in ("experiment_001-systematic", "experiment_001-degen"):
            await conn.execute(
                "INSERT INTO paper_runs(name, started_at, starting_balance_cents, "
                "current_balance_cents, config_json, profile_name, tag) "
                "VALUES (?, '2026-07-01T00:00:00Z', 1000000, 1000000, '{}', ?, ?)",
                (name, name.rsplit("-", 1)[-1], "experiment_001"),
            )
        migration = (
            Path(__file__).parents[2]
            / "src"
            / "polysim"
            / "db"
            / "migrations"
            / "0013_retire_legacy_experiment.sql"
        )
        await conn.executescript(migration.read_text(encoding="utf-8"))
        rows = await conn.execute_fetchall(
            "SELECT paused_at, pause_reason FROM paper_runs "
            "WHERE tag = 'experiment_001' ORDER BY id"
        )
    assert len(rows) == 2
    assert all(row[0] is not None for row in rows)
    assert all(row[1] == "strategy_set_v3: legacy experiment retired" for row in rows)
