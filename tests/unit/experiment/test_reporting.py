"""Experiment reporting tests — empirical-priors §8.5.

We test:
  * `gather_experiment_data` reads paper_runs / paper_positions / wallets_discovery
    / decision_rejections.jsonl and groups them correctly.
  * `render_markdown` emits all 6 hypothesis rows plus a verdict table that
    survives the empty-data path (insufficient sample → ambiguous, never crash).
  * `run_all_hypotheses` returns six TestResults in canonical order.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite
import pytest

from polysim.experiment.reporting import (
    ExperimentData,
    ExperimentSummary,
    HypothesisInputs,
    gather_experiment_data,
    render_markdown,
    run_all_hypotheses,
)


async def _build_db(path: Path) -> int:
    """Build a minimal experiment-shaped DB. Returns experiment_id."""
    async with aiosqlite.connect(str(path)) as db:
        await db.executescript(
            """
            CREATE TABLE experiments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                version TEXT NOT NULL,
                niche_tags_version TEXT NOT NULL,
                belief_schema_version TEXT NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                cohort_frozen_at TEXT,
                cohort_hash TEXT,
                cohort_size INTEGER,
                notes TEXT
            );
            CREATE TABLE wallets_discovery (
                address TEXT PRIMARY KEY,
                first_seen_at TEXT NOT NULL,
                is_cohort INTEGER NOT NULL DEFAULT 0,
                cohort_niche TEXT,
                edge_likelihood_global REAL,
                edge_likelihood_aec REAL,
                edge_likelihood_ai_labs REAL,
                edge_likelihood_creator_econ REAL
            );
            CREATE TABLE paper_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                profile_name TEXT NOT NULL DEFAULT 'systematic',
                tag TEXT,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                starting_balance_cents INTEGER NOT NULL,
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
                source_wallet TEXT,
                status TEXT NOT NULL
            );
            """
        )
        cur = await db.execute(
            "INSERT INTO experiments(name, version, niche_tags_version, "
            "belief_schema_version, started_at, cohort_hash, cohort_size) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "experiment_test", "v1", "1.0.0", "1.0.0",
                "2026-04-20T00:00:00+00:00",
                "abc123def456" + "0" * 52, 5,
            ),
        )
        exp_id = int(cur.lastrowid or 0)
        await db.executemany(
            "INSERT INTO wallets_discovery(address, first_seen_at, is_cohort, "
            "cohort_niche, edge_likelihood_global) VALUES (?, ?, 1, ?, ?)",
            [
                ("0xaec1", "2026-01-01", "aec", 0.80),
                ("0xai1",  "2026-01-01", "ai_labs", 0.75),
                ("0xgen1", "2026-01-01", "general", 0.60),
                ("0xgen2", "2026-01-01", "general", 0.55),
            ],
        )
        cur = await db.execute(
            "INSERT INTO paper_runs(name, profile_name, started_at, "
            "starting_balance_cents, current_balance_cents) "
            "VALUES (?, 'systematic', ?, ?, ?)",
            ("sys_run", "2026-04-20", 100_000_00, 100_500_00),
        )
        sys_run_id = int(cur.lastrowid or 0)
        # Three closed positions in systematic run.
        for src, pnl, day in [
            ("0xaec1",  +500, "2026-04-21"),
            ("0xgen1",  -200, "2026-04-22"),
            ("0xgen2",  +300, "2026-04-22"),
        ]:
            await db.execute(
                "INSERT INTO paper_positions(run_id, market_id, outcome, "
                "size_shares, avg_entry_price_cents, opened_at, closed_at, "
                "realized_pnl_cents, source_wallet, status) "
                "VALUES (?, ?, 'YES', 100, 50, ?, ?, ?, ?, 'CLOSED')",
                (sys_run_id, "m1", day + "T00:00:00+00:00",
                 day + "T00:00:00+00:00", pnl, src),
            )
        await db.commit()
        return exp_id


@pytest.mark.asyncio
async def test_gather_reads_db_and_classifies_pnl(tmp_path: Path) -> None:
    db = tmp_path / "exp.db"
    rejections = tmp_path / "rejections.jsonl"
    rejections.write_text(
        json.dumps({
            "first_failure": "b_resolution_risk",
            "counterfactual_pnl_cents": -150,
        }) + "\n"
        + json.dumps({
            "first_failure": "d_directional",
            "counterfactual_pnl_cents": +250,
        }) + "\n",
        encoding="utf-8",
    )
    exp_id = await _build_db(db)
    data = await gather_experiment_data(
        db, experiment_id=exp_id, rejections_log=rejections,
    )

    assert data.summary.cohort_size == 5
    assert data.summary.paper_runs == 1
    assert data.summary.paper_positions_closed == 3
    assert data.summary.realized_pnl_cents == 600  # 500 - 200 + 300
    assert data.summary.rejections_total == 2
    # Funnel: 1 each of two gates.
    assert data.summary.rejections_by_gate == {
        "b_resolution_risk": 1, "d_directional": 1,
    }
    # Niche split — 1 aec position vs 2 general.
    assert data.inputs.pnl_niche == [500.0]
    assert sorted(data.inputs.pnl_general) == [-200.0, 300.0]
    # Edge-likelihood pairs: every closed position whose source wallet is in
    # wallets_discovery.
    assert len(data.inputs.edge_likelihoods) == 3
    assert len(data.inputs.forward_pnl) == 3
    # Counterfactuals from rejections log.
    assert sorted(data.inputs.pnl_vetoed_counterfactual) == [-150.0, 250.0]


@pytest.mark.asyncio
async def test_gather_uses_most_recent_when_id_omitted(tmp_path: Path) -> None:
    db = tmp_path / "exp.db"
    await _build_db(db)
    rejections = tmp_path / "no_rejects.jsonl"  # missing path → []
    data = await gather_experiment_data(
        db, experiment_id=None, rejections_log=rejections,
    )
    assert data.summary.name == "experiment_test"
    assert data.summary.rejections_total == 0


@pytest.mark.asyncio
async def test_gather_raises_when_no_experiments(tmp_path: Path) -> None:
    db = tmp_path / "empty.db"
    async with aiosqlite.connect(str(db)) as conn:
        await conn.execute(
            "CREATE TABLE experiments (id INTEGER PRIMARY KEY, "
            "name TEXT, version TEXT, niche_tags_version TEXT, "
            "belief_schema_version TEXT, started_at TEXT, ended_at TEXT, "
            "cohort_frozen_at TEXT, cohort_hash TEXT, cohort_size INTEGER, "
            "notes TEXT)"
        )
        await conn.execute(
            "CREATE TABLE wallets_discovery (address TEXT PRIMARY KEY, "
            "first_seen_at TEXT, is_cohort INTEGER, cohort_niche TEXT, "
            "edge_likelihood_global REAL, edge_likelihood_aec REAL, "
            "edge_likelihood_ai_labs REAL, edge_likelihood_creator_econ REAL)"
        )
        await conn.execute(
            "CREATE TABLE paper_runs (id INTEGER PRIMARY KEY, name TEXT, "
            "profile_name TEXT, tag TEXT, started_at TEXT, ended_at TEXT, "
            "starting_balance_cents INTEGER, current_balance_cents INTEGER)"
        )
        await conn.execute(
            "CREATE TABLE paper_positions (id INTEGER PRIMARY KEY, "
            "run_id INTEGER, market_id TEXT, outcome TEXT, size_shares INTEGER, "
            "avg_entry_price_cents INTEGER, opened_at TEXT, closed_at TEXT, "
            "realized_pnl_cents INTEGER, source_wallet TEXT, status TEXT)"
        )
        await conn.commit()
    with pytest.raises(RuntimeError, match="No experiments"):
        await gather_experiment_data(db)


def _empty_data() -> ExperimentData:
    return ExperimentData(
        summary=ExperimentSummary(
            experiment_id=99, name="empty",
            cohort_size=0, cohort_hash=None,
            started_at="2026-04-20T00:00:00+00:00",
            ended_at=None,
            paper_runs=0,
            paper_positions_open=0, paper_positions_closed=0,
            paper_positions_resolved=0,
            realized_pnl_cents=0,
            rejections_total=0, rejections_by_gate={},
        ),
        inputs=HypothesisInputs(),
    )


def test_run_all_returns_six_tests_in_order() -> None:
    results = run_all_hypotheses(_empty_data())
    assert [r.hypothesis_id for r in results] == [
        "H1", "H2", "H3", "H4", "H5", "H6",
    ]
    # Empty data → every test ambiguous, none crash.
    for r in results:
        assert r.verdict == "ambiguous"


def test_render_markdown_includes_every_hypothesis_row() -> None:
    data = _empty_data()
    results = run_all_hypotheses(data)
    md = render_markdown(
        data, results, rendered_at=datetime(2026, 4, 24, tzinfo=UTC)
    )
    assert "Experiment Report" in md
    assert "Pre-registered hypotheses" in md
    for hid in ("H1", "H2", "H3", "H4", "H5", "H6"):
        assert hid in md
    assert "Decision-gate funnel" in md
    assert "No rejections logged yet" in md


def test_render_markdown_handles_populated_summary() -> None:
    data = ExperimentData(
        summary=ExperimentSummary(
            experiment_id=1, name="exp_1",
            cohort_size=42, cohort_hash="abcdef0123456789",
            started_at="2026-04-20T00:00:00+00:00",
            ended_at="2026-04-23T00:00:00+00:00",
            paper_runs=2,
            paper_positions_open=1, paper_positions_closed=3,
            paper_positions_resolved=4,
            realized_pnl_cents=12_345,
            rejections_total=5,
            rejections_by_gate={
                "b_resolution_risk": 3, "d_directional": 2,
            },
        ),
        inputs=HypothesisInputs(),
    )
    results = run_all_hypotheses(data)
    md = render_markdown(data, results)
    assert "exp_1" in md
    assert "$+123.45" in md or "$123.45" in md
    assert "b_resolution_risk" in md
    assert "d_directional" in md
