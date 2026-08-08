"""Fetch, cache, persist, and expose pre-trade risk intelligence."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite

from polysim.config import EvidenceConfig
from polysim.models import Market
from polysim.risk_intelligence.gates import CorrelationContext, RiskDecision
from polysim.risk_intelligence.providers import (
    EvidenceProvider,
    FallbackEvidenceProvider,
    GDELTProvider,
    GoogleNewsRSSProvider,
)
from polysim.risk_intelligence.schema import EvidenceAssessment, EvidenceSource
from polysim.risk_intelligence.scoring import (
    assess_market_evidence,
    catalyst_query_terms,
    extract_subject_key,
    is_catalyst_sensitive,
)
from polysim.utils.time import iso, now_utc, parse_iso

log = logging.getLogger(__name__)


def provider_from_config(cfg: EvidenceConfig) -> EvidenceProvider | None:
    if not cfg.enabled or cfg.provider == "none":
        return None
    if cfg.provider == "gdelt":
        return FallbackEvidenceProvider(
            (
                GDELTProvider(timeout_s=cfg.request_timeout_s),
                GoogleNewsRSSProvider(timeout_s=cfg.request_timeout_s),
            )
        )
    return None


def _source_from_dict(value: Any) -> EvidenceSource | None:
    if not isinstance(value, dict):
        return None
    try:
        return EvidenceSource.model_validate(value)
    except (TypeError, ValueError):
        return None


def _assessment_from_row(row: aiosqlite.Row) -> EvidenceAssessment:
    try:
        raw_sources = json.loads(str(row["sources_json"] or "[]"))
    except json.JSONDecodeError:
        raw_sources = []
    sources = tuple(
        source
        for source in (_source_from_dict(value) for value in raw_sources)
        if source is not None
    )
    return EvidenceAssessment(
        market_id=str(row["market_id"]),
        subject_key=(str(row["subject_key"]) if row["subject_key"] else None),
        assessed_at=parse_iso(str(row["assessed_at"])),
        provider=str(row["provider"]),
        assessment_kind=str(row["assessment_kind"]),  # type: ignore[arg-type]
        catalyst_sensitive=bool(row["catalyst_sensitive"]),
        status=str(row["status"]),  # type: ignore[arg-type]
        source_count=int(row["source_count"]),
        relevant_source_count=int(row["relevant_source_count"]),
        independent_domain_count=int(row["independent_domain_count"]),
        primary_source_count=int(row["primary_source_count"]),
        high_quality_source_count=int(row["high_quality_source_count"]),
        rumor_article_count=int(row["rumor_article_count"]),
        confirmation_article_count=int(row["confirmation_article_count"]),
        source_quality_score=float(row["source_quality_score"]),
        rumor_risk_score=float(row["rumor_risk_score"]),
        information_asymmetry_score=float(row["information_asymmetry_score"]),
        fair_probability_yes=(
            float(row["fair_probability_yes"]) if row["fair_probability_yes"] is not None else None
        ),
        probability_confidence=float(row["probability_confidence"]),
        summary=str(row["summary"]),
        sources=sources,
        assessment_id=int(row["id"]),
    )


async def write_assessment(db_path: Path, assessment: EvidenceAssessment) -> int:
    async with aiosqlite.connect(str(db_path)) as db:
        cur = await db.execute(
            """
            INSERT INTO market_evidence_assessments(
                market_id, subject_key, assessed_at, provider, assessment_kind,
                catalyst_sensitive, status, source_count,
                relevant_source_count, independent_domain_count, primary_source_count,
                high_quality_source_count, rumor_article_count, confirmation_article_count,
                source_quality_score, rumor_risk_score,
                information_asymmetry_score, fair_probability_yes,
                probability_confidence, summary, sources_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                assessment.market_id,
                assessment.subject_key,
                iso(assessment.assessed_at),
                assessment.provider,
                assessment.assessment_kind,
                int(assessment.catalyst_sensitive),
                assessment.status,
                assessment.source_count,
                assessment.relevant_source_count,
                assessment.independent_domain_count,
                assessment.primary_source_count,
                assessment.high_quality_source_count,
                assessment.rumor_article_count,
                assessment.confirmation_article_count,
                assessment.source_quality_score,
                assessment.rumor_risk_score,
                assessment.information_asymmetry_score,
                assessment.fair_probability_yes,
                assessment.probability_confidence,
                assessment.summary,
                json.dumps(
                    [source.model_dump(mode="json") for source in assessment.sources],
                    default=str,
                ),
            ),
        )
        assessment_id = int(cur.lastrowid or 0)
        await cur.close()
        await db.commit()
    return assessment_id


async def latest_assessment(
    db_path: Path,
    market_id: str,
    *,
    max_age_hours: float,
    now: datetime | None = None,
) -> EvidenceAssessment | None:
    if not db_path.exists():
        return None
    now = now or now_utc()
    cutoff = iso(now - timedelta(hours=max_age_hours))
    try:
        async with aiosqlite.connect(str(db_path)) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM market_evidence_assessments "
                "WHERE market_id = ? AND assessed_at >= ? "
                "ORDER BY CASE assessment_kind WHEN 'analyst' THEN 0 ELSE 1 END, "
                "assessed_at DESC LIMIT 1",
                (market_id, cutoff),
            ) as cur:
                row = await cur.fetchone()
    except aiosqlite.OperationalError:
        return None
    return _assessment_from_row(row) if row is not None else None


async def write_analyst_assessment(
    db_path: Path,
    market: Market,
    *,
    fair_probability_yes: float,
    probability_confidence: float,
    summary: str,
    base: EvidenceAssessment | None = None,
) -> int:
    now = now_utc()
    if base is None:
        base = assess_market_evidence(market, [], provider="analyst", now=now)
    assessment = base.model_copy(
        update={
            "assessed_at": now,
            "provider": "analyst",
            "assessment_kind": "analyst",
            "fair_probability_yes": fair_probability_yes,
            "probability_confidence": probability_confidence,
            "summary": summary,
            "assessment_id": None,
        }
    )
    return await write_assessment(db_path, assessment)


class RiskIntelligenceService:
    """Shared per-process service; locks prevent duplicate fan-out scans."""

    def __init__(
        self,
        db_path: Path,
        cfg: EvidenceConfig,
        provider: EvidenceProvider | None,
    ) -> None:
        self._db = db_path
        self._cfg = cfg
        self._provider = provider
        self._locks: dict[str, asyncio.Lock] = {}

    async def assessment_for(self, market: Market) -> EvidenceAssessment:
        if not is_catalyst_sensitive(market.question):
            return assess_market_evidence(market, [], provider="not_required", now=now_utc())

        cached = await latest_assessment(
            self._db,
            market.id,
            max_age_hours=self._cfg.max_age_hours,
        )
        if cached is not None:
            return cached

        lock = self._locks.setdefault(market.id, asyncio.Lock())
        async with lock:
            cached = await latest_assessment(
                self._db,
                market.id,
                max_age_hours=self._cfg.max_age_hours,
            )
            if cached is not None:
                return cached
            subject = extract_subject_key(market.question)
            articles = None
            if self._provider is not None and subject:
                articles = await self._provider.fetch_articles(
                    subject,
                    keywords=catalyst_query_terms(market.question),
                    limit=self._cfg.max_articles,
                    lookback_days=self._cfg.lookback_days,
                )
            assessment = assess_market_evidence(
                market,
                articles or [],
                provider=(self._provider.name if self._provider else "unavailable"),
                now=now_utc(),
            )
            if articles is None:
                assessment = assessment.model_copy(
                    update={
                        "status": "insufficient",
                        "information_asymmetry_score": 1.0,
                        "summary": "insufficient: evidence provider unavailable or failed",
                    }
                )
            assessment_id = await write_assessment(self._db, assessment)
            return assessment.model_copy(update={"assessment_id": assessment_id})

    async def correlation_context(
        self,
        *,
        run_id: int,
        market_id: str,
        market_question: str,
    ) -> CorrelationContext:
        subject = extract_subject_key(market_question)
        if not subject or not self._db.exists():
            return CorrelationContext(subject_key=subject)
        try:
            async with aiosqlite.connect(str(self._db)) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(
                    "SELECT p.id, p.size_shares, p.avg_entry_price_cents, "
                    "m.question FROM paper_positions p "
                    "JOIN markets m ON m.id = p.market_id "
                    "WHERE p.run_id = ? AND p.status = 'OPEN' "
                    "AND p.market_id <> ?",
                    (run_id, market_id),
                ) as cur:
                    rows = await cur.fetchall()
        except aiosqlite.OperationalError:
            return CorrelationContext(subject_key=subject)
        matched = [
            row for row in rows if extract_subject_key(str(row["question"] or "")) == subject
        ]
        return CorrelationContext(
            subject_key=subject,
            open_positions=len(matched),
            exposure_cents=sum(
                int(row["size_shares"] or 0) * int(row["avg_entry_price_cents"] or 0)
                for row in matched
            ),
            position_ids=tuple(int(row["id"]) for row in matched),
        )

    async def record_decision(
        self,
        *,
        run_id: int,
        flag_id: int | None,
        market_id: str,
        assessment: EvidenceAssessment | None,
        decision: RiskDecision,
    ) -> int:
        async with aiosqlite.connect(str(self._db)) as db:
            cur = await db.execute(
                """
                INSERT INTO trade_risk_decisions(
                    run_id, flag_id, market_id, assessment_id, policy,
                    subject_key, catalyst_sensitive, evidence_status,
                    correlated_positions, subject_exposure_cents,
                    entry_price_cents, fee_cents, spread_cents,
                    fair_value_cents, uncertainty_penalty_cents,
                    edge_after_cost_cents, passed, gate_name, reason,
                    detail_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    flag_id,
                    market_id,
                    assessment.assessment_id if assessment else None,
                    decision.policy,
                    decision.subject_key,
                    int(decision.catalyst_sensitive),
                    decision.evidence_status,
                    decision.correlated_positions,
                    decision.subject_exposure_cents,
                    decision.entry_price_cents,
                    decision.fee_cents,
                    decision.spread_cents,
                    decision.fair_value_cents,
                    decision.uncertainty_penalty_cents,
                    decision.edge_after_cost_cents,
                    int(decision.passed),
                    decision.gate_name,
                    decision.reason,
                    json.dumps(decision.detail, default=str),
                    iso(now_utc()),
                ),
            )
            decision_id = int(cur.lastrowid or 0)
            await cur.close()
            await db.commit()
        return decision_id

    async def attach_position(self, decision_id: int, position_id: int) -> None:
        async with aiosqlite.connect(str(self._db)) as db:
            await db.execute(
                "UPDATE trade_risk_decisions SET position_id = ? WHERE id = ?",
                (position_id, decision_id),
            )
            await db.commit()


async def recent_risk_decisions(
    db_path: Path,
    *,
    limit: int = 12,
) -> list[dict[str, Any]]:
    if not db_path.exists():
        return []
    try:
        async with aiosqlite.connect(str(db_path)) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT d.id, d.created_at, d.run_id, d.flag_id, d.market_id, "
                "d.position_id, d.policy, d.subject_key, d.catalyst_sensitive, "
                "d.evidence_status, d.correlated_positions, "
                "d.edge_after_cost_cents, d.passed, d.gate_name, d.reason, "
                "m.question FROM trade_risk_decisions d "
                "LEFT JOIN markets m ON m.id = d.market_id "
                "ORDER BY d.created_at DESC, d.id DESC LIMIT ?",
                (limit,),
            ) as cur:
                rows = await cur.fetchall()
    except aiosqlite.OperationalError:
        return []
    return [dict(row) for row in rows]


async def recent_evidence_assessments(
    db_path: Path,
    *,
    limit: int = 8,
) -> list[dict[str, Any]]:
    if not db_path.exists():
        return []
    try:
        async with aiosqlite.connect(str(db_path)) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT e.id, e.assessed_at, e.market_id, e.subject_key, "
                "e.provider, e.assessment_kind, e.status, e.source_count, "
                "e.relevant_source_count, e.confirmation_article_count, "
                "e.source_quality_score, e.rumor_risk_score, "
                "e.information_asymmetry_score, e.fair_probability_yes, "
                "e.probability_confidence, e.summary, m.question "
                "FROM market_evidence_assessments e "
                "LEFT JOIN markets m ON m.id = e.market_id "
                "ORDER BY e.assessed_at DESC, e.id DESC LIMIT ?",
                (limit,),
            ) as cur:
                rows = await cur.fetchall()
    except aiosqlite.OperationalError:
        return []
    return [dict(row) for row in rows]


async def risk_decision_counts_24h(db_path: Path) -> dict[str, int]:
    if not db_path.exists():
        return {"passed": 0, "blocked": 0}
    cutoff = iso(now_utc() - timedelta(hours=24))
    try:
        async with (
            aiosqlite.connect(str(db_path)) as db,
            db.execute(
                "SELECT COALESCE(SUM(passed), 0), "
                "COALESCE(SUM(CASE WHEN passed = 0 THEN 1 ELSE 0 END), 0) "
                "FROM trade_risk_decisions WHERE created_at >= ?",
                (cutoff,),
            ) as cur,
        ):
            row = await cur.fetchone()
    except aiosqlite.OperationalError:
        return {"passed": 0, "blocked": 0}
    return {
        "passed": int(row[0] or 0) if row else 0,
        "blocked": int(row[1] or 0) if row else 0,
    }


__all__ = [
    "RiskIntelligenceService",
    "latest_assessment",
    "provider_from_config",
    "recent_evidence_assessments",
    "recent_risk_decisions",
    "risk_decision_counts_24h",
    "write_analyst_assessment",
    "write_assessment",
]
