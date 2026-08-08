"""Deterministic source-quality, rumor-risk, and subject scoring."""

from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import urlparse

from polysim.models import Market
from polysim.risk_intelligence.schema import (
    EvidenceArticle,
    EvidenceAssessment,
    EvidenceSource,
    SourceTier,
)
from polysim.utils.time import now_utc

_HIGH_QUALITY_DOMAINS = {
    "abcnews.go.com",
    "apnews.com",
    "axios.com",
    "bbc.co.uk",
    "bbc.com",
    "bloomberg.com",
    "cbsnews.com",
    "cnn.com",
    "cnbc.com",
    "economist.com",
    "ft.com",
    "nbcnews.com",
    "npr.org",
    "nytimes.com",
    "politico.com",
    "reuters.com",
    "thehill.com",
    "usatoday.com",
    "washingtonpost.com",
    "wsj.com",
}
_LOW_QUALITY_DOMAINS = {
    "facebook.com",
    "freerepublic.com",
    "reddit.com",
    "rumble.com",
    "tiktok.com",
    "twitter.com",
    "wnd.com",
    "x.com",
    "youtube.com",
}
_RUMOR_MARKERS = (
    "alleges",
    "brain dead",
    "claims",
    "cover-up",
    "mystery",
    "questions mount",
    "reportedly",
    "rumor",
    "speculation",
    "unconfirmed",
    "without evidence",
)
_CONFIRMATION_MARKERS = (
    "announces",
    "confirmed",
    "confirms",
    "court filing",
    "official statement",
    "records show",
    "resigns",
    "roll call",
    "statement from",
    "votes",
)
_CATALYST_PATTERNS = (
    "arrested",
    "charged",
    "dies",
    "fired",
    "health",
    "hospital",
    "indicted",
    "leave office",
    "miss",
    "play by",
    "resign",
    "return by",
    "attend",
    "appear",
    "step down",
    "vacate",
    "vote by",
    "votes in",
)
_CATALYST_FAMILIES: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "departure": (
        ("leave office", "resign", "step down", "vacate"),
        (
            "departure",
            "leaves office",
            "resign",
            "resignation",
            "retire",
            "step down",
            "vacate",
        ),
    ),
    "health": (
        ("dies", "health", "hospital"),
        (
            "brain dead",
            "death",
            "dies",
            "health",
            "hospital",
            "illness",
            "medical",
        ),
    ),
    "legal": (
        ("arrested", "charged", "indicted"),
        (
            "arrest",
            "charge",
            "court",
            "indict",
            "prosecut",
        ),
    ),
    "attendance": (
        ("appear", "attend", "miss", "return by", "vote by", "votes in"),
        (
            "absent",
            "attendance",
            "miss",
            "return",
            "roll call",
            "vote",
        ),
    ),
    "employment": (
        ("fired",),
        ("dismiss", "fire", "fired", "removed", "termination"),
    ),
    "participation": (
        ("play by",),
        ("available", "injury", "lineup", "play", "return"),
    ),
}
_PERSON_STATUS_TERMS = (
    "brain dead",
    "death",
    "dies",
    "health",
    "hospital",
    "illness",
    "medical",
)
_LEADING_QUESTION_WORDS = {
    "can",
    "could",
    "did",
    "do",
    "does",
    "has",
    "is",
    "was",
    "will",
    "would",
}
_LEADING_ROLE_WORDS = {
    "chairman",
    "chancellor",
    "congressman",
    "congresswoman",
    "governor",
    "leader",
    "majority",
    "minister",
    "minority",
    "president",
    "prime",
    "representative",
    "secretary",
    "senate",
    "senator",
    "speaker",
    "vice",
}
_TRAILING_NAME_SUFFIXES = {"ii", "iii", "iv", "jr", "sr"}
_NAME_RE = re.compile(
    r"\b(?:[A-Z][A-Za-z'-]{1,}|[A-Z]{2,})"
    r"(?:\s+(?:[A-Z][A-Za-z'-]{1,}|[A-Z]{2,})){1,3}\b"
)


def normalize_domain(domain_or_url: str) -> str:
    value = domain_or_url.strip().lower()
    if "://" in value:
        value = urlparse(value).netloc.lower()
    return value.removeprefix("www.").split(":", 1)[0]


def source_tier(domain_or_url: str) -> SourceTier:
    domain = normalize_domain(domain_or_url)
    if domain.endswith(".gov") or domain.endswith(".mil"):
        return "primary"
    if domain in _HIGH_QUALITY_DOMAINS:
        return "high"
    if domain in _LOW_QUALITY_DOMAINS:
        return "low"
    return "medium"


def extract_subject_key(question: str) -> str | None:
    """Return a stable named-subject key, such as ``mitch mcconnell``."""
    candidates: list[str] = []
    for match in _NAME_RE.findall(question):
        words = match.split()
        while words and words[0].lower() in _LEADING_QUESTION_WORDS:
            words.pop(0)
        while words and words[0].lower().rstrip(".") in _LEADING_ROLE_WORDS:
            words.pop(0)
        while words and words[-1].lower().rstrip(".") in _TRAILING_NAME_SUFFIXES:
            words.pop()
        if len(words) >= 2:
            candidates.append(" ".join(words).lower())
    if not candidates:
        return None
    candidates.sort(key=lambda value: (len(value.split()), len(value)), reverse=True)
    return candidates[0]


def is_catalyst_sensitive(question: str) -> bool:
    text = " ".join(question.lower().split())
    legislative_vote = "vote" in text and any(
        body in text for body in ("congress", "house", "parliament", "senate")
    )
    return extract_subject_key(question) is not None and (
        legislative_vote or any(pattern in text for pattern in _CATALYST_PATTERNS)
    )


def catalyst_query_terms(question: str) -> tuple[str, ...]:
    """Return event-family terms that keep a source search claim-specific."""
    text = " ".join(question.lower().split())
    terms: list[str] = []
    for name, (question_markers, evidence_terms) in _CATALYST_FAMILIES.items():
        legislative_vote = name == "attendance" and "vote" in text and any(
            body in text for body in ("congress", "house", "parliament", "senate")
        )
        if legislative_vote or any(marker in text for marker in question_markers):
            terms.extend(evidence_terms)
    if is_catalyst_sensitive(question):
        terms.extend(_PERSON_STATUS_TERMS)
    return tuple(dict.fromkeys(terms))


def _markers(title: str, candidates: tuple[str, ...]) -> tuple[str, ...]:
    lower = title.lower()
    return tuple(marker for marker in candidates if marker in lower)


def annotate_article(
    article: EvidenceArticle,
    *,
    subject_key: str | None = None,
    catalyst_terms: tuple[str, ...] = (),
) -> EvidenceSource:
    title = article.title.lower()
    subject_tokens = {
        token for token in re.findall(r"[a-z0-9']+", subject_key or "") if len(token) >= 4
    }
    catalyst_markers = _markers(article.title, catalyst_terms)
    subject_match = not subject_tokens or any(token in title for token in subject_tokens)
    return EvidenceSource(
        **article.model_dump(),
        tier=source_tier(article.domain or article.url),
        relevant_to_catalyst=bool(subject_match and catalyst_markers),
        catalyst_markers=catalyst_markers,
        rumor_markers=_markers(article.title, _RUMOR_MARKERS),
        confirmation_markers=_markers(article.title, _CONFIRMATION_MARKERS),
    )


def assess_market_evidence(
    market: Market,
    articles: list[EvidenceArticle],
    *,
    provider: str,
    now: datetime | None = None,
) -> EvidenceAssessment:
    now = now or now_utc()
    sensitive = is_catalyst_sensitive(market.question)
    subject = extract_subject_key(market.question)
    event_terms = catalyst_query_terms(market.question)
    sources = tuple(
        annotate_article(
            article,
            subject_key=subject,
            catalyst_terms=event_terms,
        )
        for article in articles
    )
    count = len(sources)
    relevant = tuple(source for source in sources if source.relevant_to_catalyst)
    relevant_count = len(relevant)
    domains = {normalize_domain(source.domain or source.url) for source in relevant}
    primary = sum(source.tier == "primary" for source in relevant)
    high = sum(source.tier == "high" for source in relevant)
    low = sum(source.tier == "low" for source in relevant)
    rumor_count = sum(bool(source.rumor_markers) for source in relevant)
    confirmation_count = sum(bool(source.confirmation_markers) for source in relevant)
    tier_weight = {"primary": 1.0, "high": 0.85, "medium": 0.5, "low": 0.1}
    quality = (
        sum(tier_weight[source.tier] for source in relevant) / relevant_count
        if relevant_count
        else 0.0
    )
    raw_rumor = rumor_count / relevant_count if relevant_count else 0.0
    low_share = low / relevant_count if relevant_count else 0.0
    rumor_risk = min(1.0, raw_rumor * 0.75 + low_share * 0.35)
    breadth = min(1.0, len(domains) / 4.0)
    asymmetry = min(
        1.0,
        max(
            0.0,
            0.55 * (1.0 - quality) + 0.30 * rumor_risk + 0.15 * (1.0 - breadth),
        ),
    )

    if not sensitive:
        status = "not_required"
    elif relevant_count == 0:
        status = "insufficient"
    elif rumor_risk >= 0.55 or (low_share >= 0.5 and primary == 0 and high == 0):
        status = "rumor_heavy"
    elif primary + high == 0 and relevant_count < 3:
        status = "insufficient"
    elif (
        (primary >= 1 or (high >= 2 and len(domains) >= 2))
        and confirmation_count >= 1
        and quality >= 0.65
        and rumor_risk <= 0.30
    ):
        status = "corroborated"
    elif primary + high >= 1:
        status = "mixed"
    else:
        status = "insufficient"

    summary = (
        f"{status}: {relevant_count}/{count} catalyst-relevant sources across "
        f"{len(domains)} domains; primary={primary}, high={high}, "
        f"confirmed={confirmation_count}, rumor-marked={rumor_count}."
    )
    return EvidenceAssessment(
        market_id=market.id,
        subject_key=subject,
        assessed_at=now,
        provider=provider,
        catalyst_sensitive=sensitive,
        status=status,  # type: ignore[arg-type]
        source_count=count,
        relevant_source_count=relevant_count,
        independent_domain_count=len(domains),
        primary_source_count=primary,
        high_quality_source_count=high,
        rumor_article_count=rumor_count,
        confirmation_article_count=confirmation_count,
        source_quality_score=quality,
        rumor_risk_score=rumor_risk,
        information_asymmetry_score=asymmetry,
        summary=summary,
        sources=sources,
    )


__all__ = [
    "annotate_article",
    "assess_market_evidence",
    "catalyst_query_terms",
    "extract_subject_key",
    "is_catalyst_sensitive",
    "normalize_domain",
    "source_tier",
]
