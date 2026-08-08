from __future__ import annotations

from datetime import UTC, datetime

from polysim.models import Market
from polysim.risk_intelligence.schema import EvidenceArticle
from polysim.risk_intelligence.scoring import (
    assess_market_evidence,
    extract_subject_key,
    is_catalyst_sensitive,
    source_tier,
)


def _market(question: str) -> Market:
    return Market(
        id="m1",
        slug="m1",
        question=question,
        category="politics",
        created_at=datetime(2026, 7, 1, tzinfo=UTC),
    )


def _article(title: str, domain: str) -> EvidenceArticle:
    return EvidenceArticle(
        title=title,
        url=f"https://{domain}/story",
        domain=domain,
        published_at=datetime(2026, 7, 10, tzinfo=UTC),
    )


def test_extracts_named_subject_and_sensitive_catalyst() -> None:
    question = "Will Mitch McConnell step down from the Senate before his term ends?"
    assert extract_subject_key(question) == "mitch mcconnell"
    assert is_catalyst_sensitive(question) is True
    assert (
        is_catalyst_sensitive("Will the Republicans win the Kentucky Senate race in 2026?") is False
    )
    assert extract_subject_key("Will Senator Mitch McConnell resign?") == "mitch mcconnell"
    assert (
        extract_subject_key("Will Senate Minority Leader Mitch McConnell resign?")
        == "mitch mcconnell"
    )


def test_source_tiers_distinguish_primary_credible_and_social() -> None:
    assert source_tier("mcconnell.senate.gov") == "primary"
    assert source_tier("apnews.com") == "high"
    assert source_tier("localpaper.example") == "medium"
    assert source_tier("x.com") == "low"


def test_rumor_heavy_coverage_does_not_become_confirmation() -> None:
    market = _market("Mitch McConnell votes in the Senate by July 31?")
    assessment = assess_market_evidence(
        market,
        [
            _article("Rumor claims Mitch McConnell is brain dead", "x.com"),
            _article("McConnell health cover-up speculation grows", "freerepublic.com"),
            _article("Unconfirmed McConnell health claims spread online", "reddit.com"),
        ],
        provider="static",
        now=datetime(2026, 7, 10, 12, tzinfo=UTC),
    )
    assert assessment.catalyst_sensitive is True
    assert assessment.status == "rumor_heavy"
    assert assessment.source_quality_score < 0.2
    assert assessment.rumor_risk_score > 0.5


def test_independent_primary_and_credible_sources_can_be_corroborated() -> None:
    market = _market("Will Mitch McConnell step down before January?")
    assessment = assess_market_evidence(
        market,
        [
            _article(
                "Official statement: Senator McConnell will step down",
                "mcconnell.senate.gov",
            ),
            _article("McConnell confirms resignation plan", "apnews.com"),
            _article("Records show McConnell departure plan", "reuters.com"),
        ],
        provider="static",
        now=datetime(2026, 7, 10, 12, tzinfo=UTC),
    )
    assert assessment.status == "corroborated"
    assert assessment.primary_source_count == 1
    assert assessment.high_quality_source_count == 2
    assert assessment.independent_domain_count == 3


def test_unrelated_credible_coverage_is_not_corroboration() -> None:
    market = _market("Will Mitch McConnell step down before January?")
    assessment = assess_market_evidence(
        market,
        [
            _article("McConnell comments on the budget", "apnews.com"),
            _article("Mitch McConnell meets Senate colleagues", "reuters.com"),
        ],
        provider="static",
        now=datetime(2026, 7, 10, 12, tzinfo=UTC),
    )
    assert assessment.status == "insufficient"
    assert assessment.source_count == 2
    assert assessment.relevant_source_count == 0
    assert all(not source.relevant_to_catalyst for source in assessment.sources)
