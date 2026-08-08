from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from polysim.config import BankrollConfig, EvidenceConfig
from polysim.db import dao
from polysim.db.migrations.runner import apply_migrations
from polysim.models import Flag, Market, TradeEvent
from polysim.paper.fill_model import FillModel
from polysim.paper.profile_executor import ProfilePaperExecutor
from polysim.profiles import load_profile
from polysim.risk_intelligence.providers import StaticEvidenceProvider
from polysim.risk_intelligence.schema import EvidenceArticle
from polysim.risk_intelligence.service import (
    RiskIntelligenceService,
    recent_risk_decisions,
    write_analyst_assessment,
)


@pytest.fixture
async def db(tmp_path: Path) -> Path:
    path = tmp_path / "risk-executor.db"
    await apply_migrations(path)
    return path


async def _seed_candidate(db: Path, *, question: str) -> int:
    await dao.upsert_market(
        db,
        Market(
            id="m1",
            slug="m1",
            question=question,
            category="politics",
            created_at=datetime(2026, 7, 1, tzinfo=UTC),
            daily_volume_usd_cents=10_000_000,
        ),
    )
    await dao.upsert_wallet_first_sight(db, "0xaf4")
    await dao.insert_trades_batch(
        db,
        [
            TradeEvent(
                id="t1",
                wallet_address="0xaf4",
                market_id="m1",
                side="BUY",
                outcome="YES",
                size_shares=100,
                price_cents=40,
                timestamp=datetime.now(UTC),
            )
        ],
    )
    flag_id = await dao.write_flag(
        db,
        Flag(
            wallet_address="0xaf4",
            market_id="m1",
            trade_id="t1",
            detector_name="CohortCopy",
            raw_score=10.0,
            composite_score=10.0,
            components={"contributing_detectors": ["CohortCopy"]},
            created_at=datetime.now(UTC),
        ),
    )
    assert flag_id is not None
    return flag_id


@pytest.mark.asyncio
async def test_rumor_heavy_sensitive_market_is_blocked_and_logged(db: Path) -> None:
    flag_id = await _seed_candidate(
        db,
        question="Will Mitch McConnell step down before January?",
    )
    profile = load_profile("systematic")
    run_id = await dao.create_paper_run(
        db,
        name="verified",
        starting_balance_cents=1_000_000,
        config_snapshot={},
        profile_name=profile.name,
        profile_snapshot=profile.model_dump(),
    )
    articles = [
        EvidenceArticle(
            title="Unconfirmed rumor claims McConnell is brain dead",
            url="https://x.com/post",
            domain="x.com",
            published_at=datetime.now(UTC),
        ),
        EvidenceArticle(
            title="Cover-up speculation spreads",
            url="https://freerepublic.com/post",
            domain="freerepublic.com",
            published_at=datetime.now(UTC),
        ),
    ]
    service = RiskIntelligenceService(
        db,
        EvidenceConfig(enabled=True),
        StaticEvidenceProvider({"mitch mcconnell": articles}),
    )
    executor = ProfilePaperExecutor(
        db,
        run_id=run_id,
        profile=profile,
        bankroll=BankrollConfig(),
        fill_model=FillModel(rng_seed=1),
        risk_intelligence=service,
    )

    assert await executor.consider_flag(flag_id) is None
    assert await dao.list_open_positions(db, run_id) == []
    decisions = await recent_risk_decisions(db)
    assert decisions[0]["passed"] == 0
    assert decisions[0]["gate_name"] == "evidence_quality"


@pytest.mark.asyncio
async def test_ordinary_market_keeps_existing_copy_path(db: Path) -> None:
    flag_id = await _seed_candidate(
        db,
        question="Will the Republicans win the Kentucky Senate race in 2026?",
    )
    profile = load_profile("systematic")
    run_id = await dao.create_paper_run(
        db,
        name="verified",
        starting_balance_cents=1_000_000,
        config_snapshot={},
        profile_name=profile.name,
        profile_snapshot=profile.model_dump(),
    )
    service = RiskIntelligenceService(
        db,
        EvidenceConfig(enabled=True),
        StaticEvidenceProvider({}),
    )
    executor = ProfilePaperExecutor(
        db,
        run_id=run_id,
        profile=profile,
        bankroll=BankrollConfig(),
        fill_model=FillModel(rng_seed=1),
        risk_intelligence=service,
    )

    position_id = await executor.consider_flag(flag_id)
    assert position_id is not None
    decisions = await recent_risk_decisions(db)
    assert decisions[0]["passed"] == 1
    assert decisions[0]["gate_name"] == "not_sensitive"


@pytest.mark.asyncio
async def test_corroborated_probability_with_post_cost_edge_can_open(db: Path) -> None:
    flag_id = await _seed_candidate(
        db,
        question="Will Mitch McConnell step down before January?",
    )
    profile = load_profile("systematic")
    run_id = await dao.create_paper_run(
        db,
        name="verified",
        starting_balance_cents=1_000_000,
        config_snapshot={},
        profile_name=profile.name,
        profile_snapshot=profile.model_dump(),
    )
    service = RiskIntelligenceService(
        db,
        EvidenceConfig(enabled=True),
        StaticEvidenceProvider(
            {
                "mitch mcconnell": [
                    EvidenceArticle(
                        title="Official statement: McConnell will step down",
                        url="https://mcconnell.senate.gov/statement",
                        domain="mcconnell.senate.gov",
                        published_at=datetime.now(UTC),
                    ),
                    EvidenceArticle(
                        title="McConnell confirms resignation plan",
                        url="https://apnews.com/story",
                        domain="apnews.com",
                        published_at=datetime.now(UTC),
                    ),
                ]
            }
        ),
    )
    market = await dao.get_market(db, "m1")
    assert market is not None
    scan = await service.assessment_for(market)
    assert scan.status == "corroborated"
    await write_analyst_assessment(
        db,
        market,
        fair_probability_yes=0.70,
        probability_confidence=0.80,
        summary="corroborated departure evidence",
        base=scan,
    )
    executor = ProfilePaperExecutor(
        db,
        run_id=run_id,
        profile=profile,
        bankroll=BankrollConfig(),
        fill_model=FillModel(rng_seed=1),
        risk_intelligence=service,
    )

    position_id = await executor.consider_flag(flag_id)
    assert position_id is not None
    decisions = await recent_risk_decisions(db)
    assert decisions[0]["passed"] == 1
    assert decisions[0]["gate_name"] == "verified_edge"
