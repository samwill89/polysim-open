"""Composite scorer — spec §7.4. Build plan §2.8 (compose), §2.9 (dedup), §2.10 (G6).

Hard rules enforced here:
  §14 #5 — reproducible: pure function of (signals, weights, threshold).
  §14 #6 — investigator never modifies scores; only `acted_on`.
  §14 #7 — detector returning NaN/Inf is quarantined (signal dropped).
  G6    — wallet profile older than 1h causes scorer to short-circuit.
  G11   — FreshWalletDetector cannot solo-flag; min_contributing_detectors >= 2.
"""

from __future__ import annotations

import logging
import math
from pathlib import Path

from polysim.db import dao
from polysim.investigator.agent import Investigator, investigate_flag
from polysim.models import CompositeScore, DetectorSignal, Flag, Wallet, WalletProfile
from polysim.profiler.wallet_profiler import is_stale
from polysim.utils.time import now_utc

log = logging.getLogger(__name__)


class CompositeScorer:
    def __init__(
        self,
        weights: dict[str, float],
        *,
        flag_threshold: float = 5.0,
        min_contributing_detectors: int = 2,
        dedup_window_seconds: int = 600,
    ) -> None:
        self.weights = weights
        self.flag_threshold = flag_threshold
        self.min_contributing_detectors = min_contributing_detectors
        self.dedup_window_seconds = dedup_window_seconds

    def compose(
        self,
        *,
        wallet_address: str,
        market_id: str,
        signals: list[DetectorSignal | None],
    ) -> CompositeScore:
        """Pure — no I/O. Return a CompositeScore aggregating valid signals."""
        valid: list[DetectorSignal] = []
        for s in signals:
            if s is None:
                continue
            if not (math.isfinite(s.raw_score) and math.isfinite(s.confidence)):
                # §14 #7 — detector produced NaN/Inf, quarantine.
                log.warning("quarantining %s: non-finite score/confidence", s.detector_name)
                continue
            valid.append(s)

        components: dict[str, float] = {}
        contributing: list[str] = []
        weighted_sum = 0.0
        for s in valid:
            w = float(self.weights.get(s.detector_name, 0.0))
            contribution = w * s.raw_score * s.confidence
            components[s.detector_name] = contribution
            if s.raw_score > 0.0 and w > 0.0:
                contributing.append(s.detector_name)
            weighted_sum += contribution

        score = max(0.0, min(10.0, weighted_sum))
        return CompositeScore(
            wallet_address=wallet_address.lower(),
            market_id=market_id,
            score=score,
            components=components,
            contributing_detectors=contributing,
            created_at=now_utc(),
        )

    def should_flag(self, composite: CompositeScore) -> bool:
        return (
            composite.score >= self.flag_threshold
            and len(composite.contributing_detectors) >= self.min_contributing_detectors
        )

    def to_flag(
        self,
        composite: CompositeScore,
        signals: list[DetectorSignal | None],
        *,
        trade_id: str | None = None,
    ) -> Flag:
        """Build the Flag row for persistence.

        Stores each detector's raw + confidence + components in components_json
        so the row is independently re-verifiable (§14 #5).
        """
        per_detector: dict[str, object] = {}
        for s in signals:
            if s is None:
                continue
            per_detector[s.detector_name] = {
                "raw_score": s.raw_score,
                "confidence": s.confidence,
                "components": s.components,
                "evidence": s.evidence,
            }
        return Flag(
            wallet_address=composite.wallet_address,
            market_id=composite.market_id,
            trade_id=trade_id,
            detector_name="composite",
            raw_score=sum(
                (s.raw_score for s in signals if s is not None), start=0.0
            ),
            composite_score=composite.score,
            components={
                "weights": self.weights,
                "contribution_by_detector": composite.components,
                "contributing_detectors": composite.contributing_detectors,
                "per_detector": per_detector,
            },
            investigator_verdict=None,
            investigator_reasoning=None,
            created_at=composite.created_at,
            acted_on=False,
        )


# ── Orchestration — profile-staleness-aware score+persist ─


async def score_and_persist(
    db_path: Path,
    scorer: CompositeScorer,
    detectors: list[object],          # Detector protocol members
    *,
    wallet_address: str,
    market_id: str,
    trade_id: str | None = None,
    investigator: Investigator | None = None,
    min_composite_to_invoke: float = 6.0,
) -> int | None:
    """Full flow: staleness guard → run each detector → compose → dedup → persist.

    When `investigator` is provided and the persisted flag's composite
    score crosses `min_composite_to_invoke`, the investigator is run
    synchronously and its verdict + flag_costs row are written back.

    Returns the new flag id if a flag was both composed and persisted, or
    None if (a) profile stale (G6), (b) no flag threshold met, or (c) a
    recent duplicate exists (dedup).
    """
    profile = await dao.get_latest_profile(db_path, wallet_address)
    if profile is None or is_stale(profile):
        return None

    market = await dao.get_market(db_path, market_id)
    if market is None:
        return None

    wallet = await dao.get_wallet(db_path, wallet_address)
    if wallet is None:
        return None

    # Attach nonce + optional mid-price estimate to the profile's features
    # so detectors can reach them without their own DB hop.
    enriched = _enrich_profile(profile, wallet)

    trade = None
    if trade_id is not None:
        # Lightweight: pull full trade history, find the one we need.
        trades = await dao.get_trades_by_wallet(db_path, wallet_address)
        trade = next((t for t in trades if t.id == trade_id), None)

    signals: list[DetectorSignal | None] = []
    for d in detectors:
        try:
            sig = await d.score(enriched, market, trade)  # type: ignore[attr-defined]
            signals.append(sig)
        except Exception as exc:
            log.warning("%s.score raised: %s", type(d).__name__, exc)
            signals.append(None)

    composite = scorer.compose(
        wallet_address=wallet_address, market_id=market_id, signals=signals
    )
    if not scorer.should_flag(composite):
        return None

    # Dedup — suppress identical (wallet, market) flag within window (2.9).
    recent = await dao.get_recent_flag(
        db_path,
        wallet_address,
        market_id,
        within_seconds=scorer.dedup_window_seconds,
    )
    if recent is not None:
        return None

    flag = scorer.to_flag(composite, signals, trade_id=trade_id)
    new_id = await dao.write_flag(db_path, flag)

    # §14 #6 — investigator is a FILTER, never a scorer. It writes verdict
    # + acted_on only; never touches composite_score/raw_score.
    if (
        new_id is not None
        and investigator is not None
        and composite.score >= min_composite_to_invoke
    ):
        try:
            await investigate_flag(db_path, investigator, new_id)
        except Exception as exc:
            log.warning("investigator post-flag run failed for #%s: %s", new_id, exc)

    return new_id


def _enrich_profile(profile: WalletProfile, wallet: Wallet) -> WalletProfile:
    """Attach wallet-level fields that detectors expect in features dict.

    Returns a pydantic-compatible copy with features augmented. We don't
    mutate in place because the profile is persisted and reused.
    """
    features = dict(profile.features)
    if wallet.nonce is not None:
        features["nonce"] = int(wallet.nonce)
    if wallet.funding_source is not None:
        features["funding_source"] = wallet.funding_source
    return profile.model_copy(update={"features": features})


__all__ = ["CompositeScorer", "score_and_persist"]
