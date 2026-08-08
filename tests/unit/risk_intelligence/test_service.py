from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from polysim.config import EvidenceConfig
from polysim.db import dao
from polysim.db.migrations.runner import apply_migrations
from polysim.models import Market
from polysim.risk_intelligence.providers import StaticEvidenceProvider
from polysim.risk_intelligence.schema import EvidenceArticle
from polysim.risk_intelligence.service import (
    RiskIntelligenceService,
    latest_assessment,
    write_analyst_assessment,
)


@pytest.fixture
async def db(tmp_path: Path) -> Path:
    path = tmp_path / "risk.db"
    await apply_migrations(path)
    return path


def _market(market_id: str, question: str) -> Market:
    return Market(
        id=market_id,
        slug=market_id,
        question=question,
        category="politics",
        created_at=datetime(2026, 7, 1, tzinfo=UTC),
    )


def _article(title: str, domain: str) -> EvidenceArticle:
    return EvidenceArticle(
        title=title,
        url=f"https://{domain}/story",
        domain=domain,
        published_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_service_caches_scan_and_analyst_probability_wins(db: Path) -> None:
    market = _market("m1", "Will Mitch McConnell step down before January?")
    await dao.upsert_market(db, market)
    provider = StaticEvidenceProvider(
        {
            "mitch mcconnell": [
                _article("Official statement: McConnell will step down", "senate.gov"),
                _article("McConnell confirms resignation plan", "apnews.com"),
                _article("Records show McConnell departure plan", "reuters.com"),
            ]
        }
    )
    service = RiskIntelligenceService(
        db,
        EvidenceConfig(enabled=True, provider="gdelt"),
        provider,
    )
    first = await service.assessment_for(market)
    second = await service.assessment_for(market)
    assert first.assessment_id is not None
    assert second.assessment_id == first.assessment_id
    assert provider.calls == 1
    assert first.relevant_source_count == 3

    analyst_id = await write_analyst_assessment(
        db,
        market,
        fair_probability_yes=0.65,
        probability_confidence=0.8,
        summary="analyst probability",
        base=first,
    )
    latest = await latest_assessment(db, "m1", max_age_hours=24)
    assert latest is not None
    assert latest.assessment_id == analyst_id
    assert latest.assessment_kind == "analyst"
    assert latest.fair_probability_yes == 0.65


@pytest.mark.asyncio
async def test_correlation_context_aggregates_same_subject(db: Path) -> None:
    existing = _market("m1", "Mitch McConnell votes in the Senate by July 31?")
    candidate = _market("m2", "Will Mitch McConnell step down before January?")
    await dao.upsert_markets(db, [existing, candidate])
    run_id = await dao.create_paper_run(
        db, name="risk", starting_balance_cents=1_000_000, config_snapshot={}
    )
    position_id = await dao.write_paper_position(
        db,
        run_id=run_id,
        market_id="m1",
        outcome="YES",
        size_shares=40,
        avg_entry_price_cents=20,
        source_flag_id=None,
        source_wallet="manual:test",
    )
    service = RiskIntelligenceService(db, EvidenceConfig(enabled=True), StaticEvidenceProvider({}))
    correlation = await service.correlation_context(
        run_id=run_id,
        market_id="m2",
        market_question=candidate.question,
    )
    assert correlation.open_positions == 1
    assert correlation.exposure_cents == 800
    assert correlation.position_ids == (position_id,)
