"""Web dashboard state tests."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from polysim.config import EvidenceConfig
from polysim.db import dao
from polysim.db.migrations.runner import apply_migrations
from polysim.models import Market
from polysim.risk_intelligence.gates import CorrelationContext, evaluate_trade_risk
from polysim.risk_intelligence.scoring import assess_market_evidence
from polysim.risk_intelligence.service import (
    RiskIntelligenceService,
    write_assessment,
)
from polysim.web.state import collect_dashboard_state


@pytest.fixture
async def db(tmp_path: Path) -> Path:
    path = tmp_path / "t.db"
    await apply_migrations(path)
    return path


async def _seed_market(db: Path) -> None:
    await dao.upsert_market(
        db,
        Market(
            id="m1",
            slug="m1",
            question="Will the test market resolve YES?",
            category="ai",  # type: ignore[arg-type]
            created_at=datetime(2026, 4, 1, tzinfo=UTC),
        ),
    )


async def _seed_open_position(db: Path, *, tag: str | None = None) -> int:
    await _seed_market(db)
    run_id = await dao.create_paper_run(
        db,
        name="paper",
        starting_balance_cents=1_000_000,
        config_snapshot={},
        tag=tag,
    )
    await dao.write_paper_position(
        db,
        run_id=run_id,
        market_id="m1",
        outcome="YES",
        size_shares=100,
        avg_entry_price_cents=40,
        source_flag_id=None,
        source_wallet="0xaf4",
    )
    await dao.adjust_run_balance(db, run_id, -4_000)
    return run_id


async def test_polymarket_run_uses_cash_plus_bid_marked_positions(
    db: Path,
) -> None:
    run_id = await _seed_open_position(db)
    await dao.write_orderbook_snapshot(
        db,
        market_id="m1",
        outcome="YES",
        asks=[{"price_cents": 45, "size_shares": 100}],
        bids=[{"price_cents": 35, "size_shares": 100}],
    )

    state = await collect_dashboard_state(db, selected_run_id=run_id)
    selected = state["selected_run"]

    assert selected["cash_balance_cents"] == 996_000
    assert selected["open_position_cost_cents"] == 4_000
    assert selected["open_position_value_cents"] == 3_500
    assert selected["account_value_cents"] == 999_500
    assert selected["pnl_cents"] == -500
    assert state["drawdown_pct"] == pytest.approx(0.0005)
    assert state["active_runs_summary"][0]["balance_cents"] == 999_500

    pos = state["open_positions"][0]
    assert pos["mark_source"] == "bid"
    assert pos["mark_price_cents"] == 35
    assert pos["mark_value_cents"] == 3_500
    assert pos["unrealized_pnl_cents"] == -500


async def test_polymarket_run_falls_back_to_entry_when_no_book(
    db: Path,
) -> None:
    run_id = await _seed_open_position(db)

    state = await collect_dashboard_state(db, selected_run_id=run_id)
    selected = state["selected_run"]

    assert selected["cash_balance_cents"] == 996_000
    assert selected["open_position_value_cents"] == 4_000
    assert selected["account_value_cents"] == 1_000_000
    assert selected["mark_to_market"]["entry_fallback_positions"] == 1
    assert state["open_positions"][0]["mark_source"] == "entry_fallback"


async def test_dashboard_surfaces_risk_decisions(db: Path) -> None:
    await _seed_market(db)
    run_id = await dao.create_paper_run(
        db, name="risk", starting_balance_cents=1_000_000, config_snapshot={}
    )
    market = await dao.get_market(db, "m1")
    assert market is not None
    assessment = assess_market_evidence(
        market,
        [],
        provider="static",
        now=datetime(2026, 7, 10, tzinfo=UTC),
    )
    assessment_id = await write_assessment(db, assessment)
    assessment = assessment.model_copy(update={"assessment_id": assessment_id})
    decision = evaluate_trade_risk(
        policy="monitor",
        assessment=assessment,
        correlation=CorrelationContext(subject_key=None),
        outcome="YES",
        shares=10,
        entry_price_cents=40,
        fee_cents=0,
        spread_cents=2,
        starting_balance_cents=1_000_000,
        max_correlated_positions=1,
        max_subject_exposure_pct=0.02,
        min_source_quality=0.6,
        max_rumor_risk=0.5,
        max_information_asymmetry=0.65,
        min_probability_confidence=0.65,
        min_edge_after_cost_cents=2.0,
        uncertainty_penalty_max_cents=10.0,
    )
    service = RiskIntelligenceService(db, EvidenceConfig(), None)
    await service.record_decision(
        run_id=run_id,
        flag_id=None,
        market_id="m1",
        assessment=assessment,
        decision=decision,
    )

    state = await collect_dashboard_state(db, selected_run_id=run_id)
    risk = state["risk_intelligence"]
    assert risk["decisions"][0]["market_id"] == "m1"
    assert risk["counts_24h"]["passed"] == 1
    assert risk["assessments"][0]["provider"] == "static"


async def test_active_summary_omits_paused_research_runs(db: Path) -> None:
    active_run_id = await _seed_open_position(db, tag="tournament_v1")
    paused_run_id = await dao.create_paper_run(
        db,
        name="retired-paper",
        starting_balance_cents=1_000_000,
        config_snapshot={},
    )
    await dao.pause_paper_run(db, paused_run_id, reason="strategy_set_v2: retired")
    for index in range(16):
        await dao.create_paper_run(
            db,
            name=f"equity-newer-{index}",
            starting_balance_cents=1_000_000,
            config_snapshot={},
            tag="equity_v1",
        )

    state = await collect_dashboard_state(db)

    assert [row["id"] for row in state["active_runs_summary"]] == [active_run_id]
    assert state["selected_run"]["id"] == active_run_id
