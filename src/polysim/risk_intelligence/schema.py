"""Structured evidence and risk-decision contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

EvidenceStatus = Literal[
    "not_required",
    "insufficient",
    "rumor_heavy",
    "mixed",
    "corroborated",
]
SourceTier = Literal["primary", "high", "medium", "low"]
AssessmentKind = Literal["scan", "analyst"]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EvidenceArticle(_Frozen):
    """One normalized result from a public-news provider."""

    title: str
    url: str
    domain: str
    published_at: datetime
    language: str | None = None
    source_country: str | None = None


class EvidenceSource(EvidenceArticle):
    """An article annotated with provenance and claim-quality markers."""

    tier: SourceTier
    relevant_to_catalyst: bool = False
    catalyst_markers: tuple[str, ...] = ()
    rumor_markers: tuple[str, ...] = ()
    confirmation_markers: tuple[str, ...] = ()


class EvidenceAssessment(_Frozen):
    """Point-in-time evidence snapshot for one market."""

    market_id: str
    subject_key: str | None
    assessed_at: datetime
    provider: str
    assessment_kind: AssessmentKind = "scan"
    catalyst_sensitive: bool
    status: EvidenceStatus
    source_count: int = Field(default=0, ge=0)
    relevant_source_count: int = Field(default=0, ge=0)
    independent_domain_count: int = Field(default=0, ge=0)
    primary_source_count: int = Field(default=0, ge=0)
    high_quality_source_count: int = Field(default=0, ge=0)
    rumor_article_count: int = Field(default=0, ge=0)
    confirmation_article_count: int = Field(default=0, ge=0)
    source_quality_score: float = Field(default=0.0, ge=0.0, le=1.0)
    rumor_risk_score: float = Field(default=0.0, ge=0.0, le=1.0)
    information_asymmetry_score: float = Field(default=0.0, ge=0.0, le=1.0)
    fair_probability_yes: float | None = Field(default=None, ge=0.0, le=1.0)
    probability_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    summary: str
    sources: tuple[EvidenceSource, ...] = ()
    assessment_id: int | None = None


__all__ = [
    "AssessmentKind",
    "EvidenceArticle",
    "EvidenceAssessment",
    "EvidenceSource",
    "EvidenceStatus",
    "SourceTier",
]
