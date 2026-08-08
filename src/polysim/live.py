"""`polysim live` — one-command orchestrator.

Wires the full loop under one asyncio process:
    ingest → profiler → scorer → investigator → dispatcher (N executors) →
    resolution closer → watchdog → daily summary → inbound Telegram bot.

Spec §2 forbids a web UI and §16 forbids live trading. This command does
*not* cross either line — it drives paper-only runs with operator-visible
state via the CLI TUI, Telegram push, and Telegram inbound commands.

Tear-down is cooperative: SIGINT/Ctrl-C sets the stop event; each
subsystem's `stop()` is awaited; the process exits cleanly.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from polysim.config import Config, Secrets
from polysim.db import dao
from polysim.equity.loop import EquityTrackLoop
from polysim.equity.variants import DEFAULT_LIVE_VARIANTS
from polysim.ingest.pipeline import IngestPipeline
from polysim.paper.fill_model import FillModel
from polysim.paper.profile_executor import Dispatcher, ProfilePaperExecutor
from polysim.paper.run_manager import start_run
from polysim.profiler.wallet_profiler import refresh_stale_profiles
from polysim.profiles import load_profile
from polysim.reporter.scheduler import (
    DailySummaryScheduler,
    default_summary_provider,
)
from polysim.reporter.telegram import AlertSink, make_sink_from_config
from polysim.reporter.telegram_bot import InboundTelegramBot
from polysim.scoring.composite import CompositeScorer, score_and_persist
from polysim.tournament import TournamentAllocatorLoop
from polysim.trading.costs import taker_fee_cents
from polysim.utils.watchdog import UptimeWatchdog

log = logging.getLogger(__name__)


@dataclass
class LiveConfig:
    profiles: list[str] = field(default_factory=lambda: ["systematic"])
    tag: str | None = None
    balance_cents: int = 1_000_000
    flag_poll_interval_s: float = 5.0
    resolution_interval_s: float = 300.0
    stop_loss_interval_s: float = 60.0
    scoring_interval_s: float = 30.0
    scoring_batch_size: int = 200
    cohort_copy_interval_s: float = 15.0
    cohort_copy_batch_size: int = 500
    cohort_copy_min_notional_cents: int = (
        1_000  # $10 minimum trade — captures p65+ of current retail-skewed cohort
    )
    discovery_refresh_interval_s: float = 6 * 3600  # 6h
    discovery_experiment_name: str = "experiment_001"
    tournament_interval_s: float = 6 * 3600  # 6h between rebalances
    tournament_spawn_interval_s: float = 7 * 24 * 3600  # spawn new variants weekly
    stall_threshold_s: int = 300
    enable_ingest: bool = True
    enable_daily_summary: bool = True
    enable_inbound_bot: bool = True
    enable_investigator: bool = True
    enable_scoring: bool = False  # detector-based — disabled by default after pivot
    enable_cohort_copy: bool = True  # NEW pivot: mirror cohort wallets
    enable_tournament: bool = True  # variant tournament — seed pool + rebalance
    enable_equity_track: bool = True  # NEW: parallel equity sentiment lane
    equity_interval_s: float = 24 * 3600  # daily reconcile
    equity_balance_cents: int = 1_000_000  # $10k per equity variant
    equity_variants: list[str] | None = field(default_factory=lambda: list(DEFAULT_LIVE_VARIANTS))
    enable_discovery_refresh: bool = False  # freeze cohort for forward validity
    enable_stop_loss_sweep: bool = True
    enable_signals: bool = False  # conversation-signal snapshots
    enable_evidence: bool = True  # news/evidence pre-trade gate
    daily_summary_tz: str = "UTC"
    daily_summary_hour: int = 9


def _fill_model_from_cfg(cfg: Config) -> FillModel:
    fm = cfg.fill_model
    return FillModel(
        detection_latency_p50_ms=fm.detection_latency_p50_ms,
        detection_latency_p95_ms=fm.detection_latency_p95_ms,
        decision_latency_p50_ms=fm.decision_latency_p50_ms,
        decision_latency_p95_ms=fm.decision_latency_p95_ms,
        slippage_ticks=fm.slippage_ticks,
        on_partial=fm.on_partial,
        fee_bps=fm.fee_bps,
        historical_pessimism_multiplier=fm.historical_pessimism_multiplier,
    )


class ScoringLoop:
    """Periodic scorer over newly-ingested trades.

    Closes the gap between IngestPipeline (writes `trades`) and
    FlagDispatcherLoop (reads `flags`): on each tick, picks up trades
    seen since the last watermark, refreshes stale wallet profiles for
    those wallets, then calls `score_and_persist` per trade. Any flag
    that crosses `min_composite_to_invoke` runs the investigator inline.

    Watermark is on `trades.timestamp` (string ISO). Ties on the boundary
    are reprocessed but score_and_persist's dedup handles that cleanly.
    """

    def __init__(
        self,
        db_path: Path,
        *,
        scorer: CompositeScorer,
        detectors: list[object],
        investigator: object | None,
        min_composite_to_invoke: float,
        interval_s: float = 30.0,
        batch_size: int = 200,
    ) -> None:
        self._db = db_path
        self._scorer = scorer
        self._detectors = detectors
        self._investigator = investigator
        self._min_invoke = min_composite_to_invoke
        self._interval = interval_s
        self._batch_size = batch_size
        self._watermark: str = ""
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.scored_total = 0
        self.flagged_total = 0

    async def start(self) -> None:
        # Seed watermark to "now" so we score forward only — we do not
        # rescore the historical ingest backlog at startup.
        self._watermark = await _max_trade_timestamp(self._db)
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="scoring-loop")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self._tick_once()
            except Exception as exc:
                log.warning("scoring tick failed: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
                return
            except TimeoutError:
                continue

    async def _tick_once(self) -> None:
        rows = await _trades_after(
            self._db,
            self._watermark,
            limit=self._batch_size,
        )
        if not rows:
            return
        # Refresh profiles for any wallet in this batch so the staleness
        # guard inside score_and_persist doesn't reject every trade.
        wallets = {str(r["wallet_address"]).lower() for r in rows}
        try:
            await refresh_stale_profiles(
                self._db,
                staleness_seconds=0,
                max_wallets=len(wallets) or 1,
            )
        except Exception as exc:
            log.warning("scoring loop: profile refresh failed: %s", exc)

        for r in rows:
            try:
                new_id = await score_and_persist(
                    self._db,
                    self._scorer,
                    self._detectors,
                    wallet_address=str(r["wallet_address"]),
                    market_id=str(r["market_id"]),
                    trade_id=str(r["id"]),
                    investigator=self._investigator,  # type: ignore[arg-type]
                    min_composite_to_invoke=self._min_invoke,
                )
                self.scored_total += 1
                if new_id is not None:
                    self.flagged_total += 1
                    log.info("scoring: flag #%d created", new_id)
            except Exception as exc:
                log.warning(
                    "score_and_persist failed for trade %s: %s",
                    r.get("id"),
                    exc,
                )
            ts = str(r["timestamp"])
            if ts > self._watermark:
                self._watermark = ts


class CopyOnCohortLoop:
    """Mirror trades from current-cohort wallets above a notional threshold.

    The pivot from detector-based scoring (which produced 0 flags over a
    week of real data): every trade by a wallet currently flagged
    `is_cohort = 1` in `wallets_discovery`, with notional >= the configured
    floor, gets a synthetic `CohortCopy` flag written. The existing
    FlagDispatcherLoop picks it up within seconds and the dispatcher
    routes it to every active run; profile-level gates (sizing, depth,
    drawdown, concentration) still apply.

    Watermark on `trades.timestamp`. Stays cheap by reading the cohort
    set once per tick.
    """

    def __init__(
        self,
        db_path: Path,
        *,
        interval_s: float = 15.0,
        batch_size: int = 500,
        min_notional_cents: int = 50_000,
        max_copy_price_cents: int = 90,
    ) -> None:
        self._db = db_path
        self._interval = interval_s
        self._batch_size = batch_size
        self._min_notional = min_notional_cents
        # Skip copying BUYs into near-certain favorites: at >=~90c the
        # favorite-longshot edge is gone, and after the bid-ask spread these
        # only churn (verified on 400 resolved markets: ~0% net ROI, 0/103
        # closed copies ever won). Holds the strategy to bets with room to run.
        self._max_copy_price = max_copy_price_cents
        self._watermark: str = ""
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.scanned_total = 0
        self.matched_total = 0
        self.flagged_total = 0
        self.skipped_price_total = 0
        self.sells_mirrored_total = 0

    async def start(self) -> None:
        self._watermark = await _max_trade_timestamp(self._db)
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="cohort-copy-loop")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self._tick_once()
            except Exception as exc:
                log.warning("cohort-copy tick failed: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
                return
            except TimeoutError:
                continue

    async def _tick_once(self) -> None:
        cohort = await _current_cohort_addresses(self._db)
        if not cohort:
            return
        rows = await _trades_after(
            self._db,
            self._watermark,
            limit=self._batch_size,
        )
        if not rows:
            return
        for r in rows:
            self.scanned_total += 1
            wallet = str(r["wallet_address"]).lower()
            ts = str(r["timestamp"])
            if wallet not in cohort:
                if ts > self._watermark:
                    self._watermark = ts
                continue
            # Need price/size to filter on notional. Pull once.
            full = await _trade_full(self._db, str(r["id"]))
            if full is None:
                continue
            notional = int(full["size_shares"]) * int(full["price_cents"])
            if notional < self._min_notional:
                if ts > self._watermark:
                    self._watermark = ts
                continue
            self.matched_total += 1
            side = str(full["side"]).upper()
            try:
                if side == "SELL":
                    # Mirror exit — close any of OUR paper positions sourced
                    # from this cohort wallet on this market+outcome.
                    closed = await _mirror_sell_close(self._db, full)
                    if closed:
                        self.sells_mirrored_total += 1
                        log.info(
                            "cohort-copy: mirrored SELL — closed %d position(s) "
                            "wallet=%s market=%s exit=%dc",
                            closed,
                            wallet[:14],
                            str(full["market_id"])[:14],
                            int(full["price_cents"]),
                        )
                elif int(full["price_cents"]) >= self._max_copy_price:
                    # Deep favorite — no edge after spread. Skip the copy.
                    self.skipped_price_total += 1
                else:
                    fid = await _write_cohort_copy_flag(self._db, full)
                    if fid is not None:
                        self.flagged_total += 1
                        log.info(
                            "cohort-copy: flag #%d wallet=%s notional=$%.2f price=%dc",
                            fid,
                            wallet[:14],
                            notional / 100.0,
                            int(full["price_cents"]),
                        )
            except Exception as exc:
                log.warning("cohort-copy tick item failed: %s", exc)
            if ts > self._watermark:
                self._watermark = ts


class DiscoveryRefreshLoop:
    """Periodically re-runs the discovery pipeline so the cohort tracks
    current top-P&L wallets above the volume floor instead of being
    frozen at experiment start."""

    def __init__(
        self,
        db_path: Path,
        *,
        experiment_name: str,
        interval_s: float = 6 * 3600,
    ) -> None:
        self._db = db_path
        self._experiment_name = experiment_name
        self._interval = interval_s
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.refreshed_total = 0
        self.last_size: int = 0

    async def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="discovery-refresh")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        # First refresh: wait one interval before kicking off so we
        # don't pile work on top of startup ingest churn.
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            return
        except TimeoutError:
            pass

        while not self._stop.is_set():
            try:
                await self._refresh_once()
            except Exception as exc:
                log.warning("discovery refresh failed: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
                return
            except TimeoutError:
                continue

    async def _refresh_once(self) -> None:
        from polysim.agents.belief_schema import SCHEMA_VERSION
        from polysim.discovery.classifier import (
            CLASSIFIER_VERSION,
            classify_population,
        )
        from polysim.discovery.cohort import (
            freeze_cohort,
            select_cohort,
            update_edge_likelihoods,
        )
        from polysim.discovery.features import (
            extract_features_for_run,
            write_features,
        )

        log.info("discovery refresh: starting...")
        features = await extract_features_for_run(self._db)
        if not features:
            log.warning("discovery refresh: no features extracted")
            return
        await write_features(self._db, features)
        scores = classify_population(features)
        await update_edge_likelihoods(self._db, scores)
        features_by_key = {(f.wallet_address, f.scope): f for f in features}
        picks = select_cohort(scores, features_by_key)
        if not picks:
            log.warning("discovery refresh: empty cohort selection")
            return
        await freeze_cohort(
            self._db,
            experiment_name=self._experiment_name,
            picks=picks,
            classifier_version=CLASSIFIER_VERSION,
            belief_schema_version=SCHEMA_VERSION,
            notes="auto-refresh by DiscoveryRefreshLoop",
        )
        self.refreshed_total += 1
        self.last_size = len(picks)
        log.info(
            "discovery refresh: cohort updated, size=%d",
            len(picks),
        )


class FlagDispatcherLoop:
    """Polls `flags` for new rows since last tick, fans to the dispatcher.

    This is a deliberately simple pattern: the scorer writes to `flags`,
    and this loop catches them on its next tick. For the ~seconds-scale
    latency a paper simulator tolerates, polling beats wiring a queue
    across process boundaries.
    """

    def __init__(
        self,
        db_path: Path,
        dispatcher: Dispatcher,
        *,
        poll_interval_s: float = 5.0,
    ) -> None:
        self._db = db_path
        self._dispatcher = dispatcher
        self._poll = poll_interval_s
        self._last_seen_id = 0
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.consumed = 0

    async def start(self) -> None:
        # Seed watermark so we only act on flags created AFTER `live` starts.
        self._last_seen_id = await _max_flag_id(self._db)
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="flag-dispatcher")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                new_ids = await _flags_above(self._db, self._last_seen_id)
                for fid in new_ids:
                    await self._dispatcher.on_flag(fid)
                    self._last_seen_id = max(self._last_seen_id, fid)
                    self.consumed += 1
            except Exception as exc:
                log.warning("flag dispatcher tick failed: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._poll)
                return
            except TimeoutError:
                continue


class SettlementSweepLoop:
    """Empirical-priors addendum §4.2.

    Periodic: clears `pending_until_iso` on positions whose settlement
    window elapsed, so the executor's effective-balance check can use
    that capital again.
    """

    def __init__(
        self,
        db_path: Path,
        *,
        interval_s: float = 5.0,
    ) -> None:
        self._db = db_path
        self._interval = interval_s
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.settled_total = 0

    async def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="settlement-sweep")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        from polysim.portfolio.settlement import sweep_settlements

        while not self._stop.is_set():
            try:
                self.settled_total += await sweep_settlements(self._db)
            except Exception as exc:
                log.warning("settlement sweep tick failed: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
                return
            except TimeoutError:
                continue


class StopLossLoop:
    """Periodic mark-to-market sweep that calls each executor's
    `sweep_stop_losses()`. Addendum §5.1."""

    def __init__(
        self,
        dispatcher: Dispatcher,
        *,
        interval_s: float = 60.0,
    ) -> None:
        self._dispatcher = dispatcher
        self._interval = interval_s
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.closed_total = 0

    async def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="stop-loss-sweep")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                for ex in self._dispatcher.executors:
                    closed = await ex.sweep_stop_losses()
                    self.closed_total += len(closed)
            except Exception as exc:
                log.warning("stop-loss sweep tick failed: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
                return
            except TimeoutError:
                continue


class ResolutionLoop:
    """Periodic: scan for markets resolved since last tick, close all open
    positions across all runs via the dispatcher."""

    def __init__(
        self,
        db_path: Path,
        dispatcher: Dispatcher,
        *,
        interval_s: float = 300.0,
    ) -> None:
        self._db = db_path
        self._dispatcher = dispatcher
        self._interval = interval_s
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="resolution-loop")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self._tick()
            except Exception as exc:
                log.warning("resolution loop tick failed: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
                return
            except TimeoutError:
                continue

    async def _tick(self) -> None:
        # Collect market_ids with OPEN positions across all live runs.
        seen: set[str] = set()
        for ex in self._dispatcher.executors:
            opens = await dao.list_open_positions(self._db, ex.run_id)
            for p in opens:
                mid = str(p.get("market_id") or "")
                if not mid:
                    continue
                seen.add(mid)
        for mid in seen:
            market = await dao.get_market(self._db, mid)
            if market is None or market.resolved_outcome is None:
                continue
            await self._dispatcher.on_resolution(mid)


class LiveOrchestrator:
    """Start / stop every subsystem as a single unit."""

    def __init__(
        self,
        *,
        db_path: Path,
        cfg: Config,
        secrets: Secrets,
        live_cfg: LiveConfig,
    ) -> None:
        self._db = db_path
        self._cfg = cfg
        self._secrets = secrets
        self._live_cfg = live_cfg

        self._sink: AlertSink = make_sink_from_config(
            enabled=cfg.telegram.enabled,
            bot_token=secrets.TELEGRAM_BOT_TOKEN,
            chat_id=secrets.TELEGRAM_CHAT_ID,
        )

        self._ingest: IngestPipeline | None = None
        self._dispatcher: Dispatcher | None = None
        self._scoring_loop: ScoringLoop | None = None
        self._cohort_copy_loop: CopyOnCohortLoop | None = None
        self._discovery_refresh_loop: DiscoveryRefreshLoop | None = None
        self._tournament_loop: TournamentAllocatorLoop | None = None
        self._equity_loop: EquityTrackLoop | None = None
        self._signal_loop: Any | None = None
        self._risk_intelligence: Any | None = None
        self._flag_loop: FlagDispatcherLoop | None = None
        self._resolution_loop: ResolutionLoop | None = None
        self._stop_loss_loop: StopLossLoop | None = None
        self._settlement_loop: SettlementSweepLoop | None = None
        self._watchdog: UptimeWatchdog | None = None
        self._scheduler: DailySummaryScheduler | None = None
        self._bot: InboundTelegramBot | None = None
        self._bot_task: asyncio.Task[None] | None = None
        self.run_ids: list[int] = []

    async def start(self) -> None:
        # Shared evidence service. Dispatcher fan-out reuses one cache/lock set,
        # so four profiles do not issue four identical news scans.
        if self._live_cfg.enable_evidence and self._cfg.evidence.enabled:
            from polysim.risk_intelligence.service import (
                RiskIntelligenceService,
            )
            from polysim.risk_intelligence.service import (
                provider_from_config as evidence_provider_from_config,
            )

            evidence_provider = evidence_provider_from_config(self._cfg.evidence)
            self._risk_intelligence = RiskIntelligenceService(
                self._db,
                self._cfg.evidence,
                evidence_provider,
            )

        # 1. Spin up paper runs (one per profile).
        profiles = [load_profile(n) for n in self._live_cfg.profiles]
        executors: list[ProfilePaperExecutor] = []
        fill_model = _fill_model_from_cfg(self._cfg)
        for p in profiles:
            run_name = f"{self._live_cfg.tag}-{p.name}" if self._live_cfg.tag else p.name
            existing = await dao.find_open_run_by_name(
                self._db, name=run_name, tag=self._live_cfg.tag
            )
            if existing is not None:
                rid = int(existing["id"])
                log.info("live: reusing open run #%d (%s) across restart", rid, run_name)
            else:
                rid = await start_run(
                    self._db,
                    self._cfg,
                    profile=p,
                    name_override=run_name,
                    balance_override_cents=self._live_cfg.balance_cents,
                    tag=self._live_cfg.tag,
                )
            self.run_ids.append(rid)
            executors.append(
                ProfilePaperExecutor(
                    self._db,
                    run_id=rid,
                    profile=p,
                    bankroll=self._cfg.bankroll,
                    fill_model=fill_model,
                    risk_intelligence=self._risk_intelligence,
                )
            )
            log.info(
                "live: run #%d profile=%s balance=%d", rid, p.name, self._live_cfg.balance_cents
            )

        self._dispatcher = Dispatcher(executors)

        # 2. Ingest pipeline (optional — tests pass enable_ingest=False).
        if self._live_cfg.enable_ingest:
            self._ingest = IngestPipeline(db_path=self._db, config=self._cfg, secrets=self._secrets)
            await self._ingest.start()

        # 3a. Scoring loop — reads new trades, runs the 5 detectors +
        #     composite, writes flags. Without this, flags never appear.
        if self._live_cfg.enable_scoring:
            from polysim.evaluator.backtest import (
                _build_detectors,
                _build_scorer,
            )

            detectors = await _build_detectors(self._db, self._cfg)
            scorer = _build_scorer(self._cfg)
            investigator = None
            if self._live_cfg.enable_investigator and self._secrets.ANTHROPIC_API_KEY:
                from polysim.investigator.agent import Investigator

                investigator = Investigator(
                    api_key=self._secrets.ANTHROPIC_API_KEY,
                    max_calls_per_day=(self._cfg.investigator.max_calls_per_day),
                )
            self._scoring_loop = ScoringLoop(
                self._db,
                scorer=scorer,
                detectors=detectors,
                investigator=investigator,
                min_composite_to_invoke=(self._cfg.investigator.min_composite_to_invoke),
                interval_s=self._live_cfg.scoring_interval_s,
                batch_size=self._live_cfg.scoring_batch_size,
            )
            await self._scoring_loop.start()

        # 3b. Cohort-copy loop — pivot from detector scoring. Mirrors any
        #     trade by a current-cohort wallet above the notional floor.
        if self._live_cfg.enable_cohort_copy:
            self._cohort_copy_loop = CopyOnCohortLoop(
                self._db,
                interval_s=self._live_cfg.cohort_copy_interval_s,
                batch_size=self._live_cfg.cohort_copy_batch_size,
                min_notional_cents=(self._live_cfg.cohort_copy_min_notional_cents),
            )
            await self._cohort_copy_loop.start()

        # 3c. Discovery refresh — keeps cohort tracking current top-P&L
        #     wallets instead of being frozen at experiment start.
        if self._live_cfg.enable_discovery_refresh:
            self._discovery_refresh_loop = DiscoveryRefreshLoop(
                self._db,
                experiment_name=self._live_cfg.discovery_experiment_name,
                interval_s=self._live_cfg.discovery_refresh_interval_s,
            )
            await self._discovery_refresh_loop.start()

        # 3c2. Tournament allocator — reconciles the compact live pool on
        #      startup, then periodically pauses losers + promotes winners.
        #      Each variant run receives the same CohortCopy flags from
        #      the existing dispatcher; divergence happens at the
        #      executor's profile-based gates.
        if self._live_cfg.enable_tournament:
            self._tournament_loop = TournamentAllocatorLoop(
                self._db,
                interval_s=self._live_cfg.tournament_interval_s,
                spawn_interval_s=self._live_cfg.tournament_spawn_interval_s,
            )
            await self._tournament_loop.start()
            # Re-fetch executors so the dispatcher knows about the newly
            # spawned tournament runs.
            for new_id in await _new_run_ids_with_tag(self._db, "tournament_v1"):
                if any(e.run_id == new_id for e in executors):
                    continue
                row = await dao.get_paper_run(self._db, new_id)
                if row is None:
                    continue
                snap = row.get("profile_snapshot_json")
                from polysim.config import RiskProfile

                snap_dict = json.loads(snap) if isinstance(snap, str) else (snap or {})
                profile = RiskProfile.model_validate(snap_dict)
                executors.append(
                    ProfilePaperExecutor(
                        self._db,
                        run_id=new_id,
                        profile=profile,
                        bankroll=self._cfg.bankroll,
                        fill_model=fill_model,
                        risk_intelligence=self._risk_intelligence,
                    )
                )
            self._dispatcher = Dispatcher(executors)

        # 3c-bis. Equity sentiment track — fully independent parallel lane
        #         (own tables, own daily loop, own $10k variant tournament).
        if self._live_cfg.enable_equity_track:
            self._equity_loop = EquityTrackLoop(
                self._db,
                interval_s=self._live_cfg.equity_interval_s,
                balance_cents=self._live_cfg.equity_balance_cents,
                variant_names=self._live_cfg.equity_variants,
            )
            await self._equity_loop.start()

        # 3c-ter. Conversation-signal snapshots (signals package) — needs
        #         BOTH the LiveConfig flag and config.signals.enabled. When
        #         off (default), signal_* tournament variants trade exactly
        #         like baseline (no signal rows → neutral multiplier).
        if self._live_cfg.enable_signals:
            from polysim.signals.service import (
                SignalSnapshotLoop,
                provider_from_config,
            )

            provider = provider_from_config(self._cfg)
            if provider is not None:
                self._signal_loop = SignalSnapshotLoop(
                    self._db,
                    self._cfg,
                    provider,
                )
                await self._signal_loop.start()
            else:
                log.info(
                    "live: signals loop requested but config.signals is "
                    "disabled or has no usable provider — skipping",
                )

        # 3d. Flag dispatcher + resolution loops.
        self._flag_loop = FlagDispatcherLoop(
            self._db,
            self._dispatcher,
            poll_interval_s=self._live_cfg.flag_poll_interval_s,
        )
        await self._flag_loop.start()
        self._resolution_loop = ResolutionLoop(
            self._db,
            self._dispatcher,
            interval_s=self._live_cfg.resolution_interval_s,
        )
        await self._resolution_loop.start()

        if self._live_cfg.enable_stop_loss_sweep:
            self._stop_loss_loop = StopLossLoop(
                self._dispatcher,
                interval_s=self._live_cfg.stop_loss_interval_s,
            )
            await self._stop_loss_loop.start()

        # Settlement sweep (§4.2) — clears pending_until_iso when due.
        self._settlement_loop = SettlementSweepLoop(
            self._db,
            interval_s=2.0,
        )
        await self._settlement_loop.start()

        # 4. Watchdog — sends a Telegram alert on stall.
        async def _stall_alert(msg: str) -> None:
            # Wrap the plain message as a "daily summary" attention flag.
            log.warning("watchdog: %s", msg)

        self._watchdog = UptimeWatchdog(
            self._db,
            alert_fn=_stall_alert,
            stall_threshold_s=self._live_cfg.stall_threshold_s,
        )
        await self._watchdog.start()

        # 5. Daily summary scheduler.
        if self._live_cfg.enable_daily_summary:
            from polysim.reporter.telegram import DailySummaryAlert

            async def _provider() -> DailySummaryAlert:
                return await default_summary_provider(self._db)

            self._scheduler = DailySummaryScheduler(
                self._sink,
                provider=_provider,
                fire_hour_local=self._live_cfg.daily_summary_hour,
                tz_name=self._live_cfg.daily_summary_tz,
            )
            await self._scheduler.start()

        # 6. Inbound Telegram bot — optional.
        if (
            self._live_cfg.enable_inbound_bot
            and self._cfg.telegram.enabled
            and self._secrets.TELEGRAM_BOT_TOKEN
            and self._secrets.TELEGRAM_CHAT_ID
        ):
            self._bot = InboundTelegramBot(
                bot_token=self._secrets.TELEGRAM_BOT_TOKEN,
                authorized_chat_id=self._secrets.TELEGRAM_CHAT_ID,
                db_path=self._db,
            )
            self._bot_task = asyncio.create_task(
                self._bot.run(),
                name="inbound-bot",
            )

    async def stop(self) -> None:
        # Reverse order.
        if self._bot is not None:
            await self._bot.stop()
        if self._bot_task is not None:
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(self._bot_task, timeout=5.0)
            self._bot_task = None
        if self._scheduler is not None:
            await self._scheduler.stop()
        if self._watchdog is not None:
            await self._watchdog.stop()
        if self._stop_loss_loop is not None:
            await self._stop_loss_loop.stop()
        if self._settlement_loop is not None:
            await self._settlement_loop.stop()
        if self._resolution_loop is not None:
            await self._resolution_loop.stop()
        if self._flag_loop is not None:
            await self._flag_loop.stop()
        if self._tournament_loop is not None:
            await self._tournament_loop.stop()
        if self._equity_loop is not None:
            await self._equity_loop.stop()
        if self._signal_loop is not None:
            await self._signal_loop.stop()
        if self._discovery_refresh_loop is not None:
            await self._discovery_refresh_loop.stop()
        if self._cohort_copy_loop is not None:
            await self._cohort_copy_loop.stop()
        if self._scoring_loop is not None:
            await self._scoring_loop.stop()
        if self._ingest is not None:
            await self._ingest.stop()


# ── tiny helpers ─────────────────────────────────────────


async def _max_flag_id(db_path: Path) -> int:
    if not db_path.exists():
        return 0
    import aiosqlite

    try:
        async with (
            aiosqlite.connect(str(db_path)) as db,
            db.execute("SELECT COALESCE(MAX(id), 0) FROM flags") as cur,
        ):
            row = await cur.fetchone()
    except aiosqlite.OperationalError:
        return 0
    return int(row[0]) if row and row[0] is not None else 0


async def _flags_above(db_path: Path, last_id: int) -> list[int]:
    if not db_path.exists():
        return []
    import aiosqlite

    try:
        async with (
            aiosqlite.connect(str(db_path)) as db,
            db.execute(
                "SELECT id FROM flags WHERE id > ? ORDER BY id ASC LIMIT 500",
                (last_id,),
            ) as cur,
        ):
            rows = await cur.fetchall()
    except aiosqlite.OperationalError:
        return []
    return [int(r[0]) for r in rows]


async def _max_trade_timestamp(db_path: Path) -> str:
    if not db_path.exists():
        return ""
    import aiosqlite

    try:
        async with (
            aiosqlite.connect(str(db_path)) as db,
            db.execute("SELECT COALESCE(MAX(timestamp), '') FROM trades") as cur,
        ):
            row = await cur.fetchone()
    except aiosqlite.OperationalError:
        return ""
    return str(row[0]) if row and row[0] is not None else ""


async def _trades_after(
    db_path: Path,
    watermark: str,
    *,
    limit: int = 200,
) -> list[dict[str, str]]:
    """Trade rows with timestamp > watermark, oldest first."""
    if not db_path.exists():
        return []
    import aiosqlite

    try:
        async with aiosqlite.connect(str(db_path)) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT id, wallet_address, market_id, timestamp "
                "FROM trades WHERE timestamp > ? "
                "ORDER BY timestamp ASC LIMIT ?",
                (watermark, limit),
            ) as cur:
                rows = await cur.fetchall()
    except aiosqlite.OperationalError:
        return []
    return [dict(r) for r in rows]


async def _new_run_ids_with_tag(db_path: Path, tag: str) -> list[int]:
    """Run IDs tagged with `tag` that are still active (ended_at IS NULL).
    Used by LiveOrchestrator to attach freshly-spawned tournament runs to
    the existing Dispatcher.
    """
    if not db_path.exists():
        return []
    import aiosqlite

    try:
        async with (
            aiosqlite.connect(str(db_path)) as db,
            db.execute(
                "SELECT id FROM paper_runs WHERE tag = ? AND ended_at IS NULL ORDER BY id ASC",
                (tag,),
            ) as cur,
        ):
            rows = await cur.fetchall()
    except aiosqlite.OperationalError:
        return []
    return [int(r[0]) for r in rows]


async def _current_cohort_addresses(db_path: Path) -> set[str]:
    """Lowercased addresses of every wallet currently flagged is_cohort=1."""
    if not db_path.exists():
        return set()
    import aiosqlite

    try:
        async with (
            aiosqlite.connect(str(db_path)) as db,
            db.execute("SELECT address FROM wallets_discovery WHERE is_cohort = 1") as cur,
        ):
            rows = await cur.fetchall()
    except aiosqlite.OperationalError:
        return set()
    return {str(r[0]).lower() for r in rows if r and r[0]}


async def _trade_full(db_path: Path, trade_id: str) -> dict[str, Any] | None:
    """Single-trade fetch with the columns CopyOnCohortLoop needs."""
    if not db_path.exists():
        return None
    import aiosqlite

    try:
        async with aiosqlite.connect(str(db_path)) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT id, wallet_address, market_id, side, outcome, "
                "size_shares, price_cents, timestamp "
                "FROM trades WHERE id = ?",
                (trade_id,),
            ) as cur:
                row = await cur.fetchone()
    except aiosqlite.OperationalError:
        return None
    return dict(row) if row else None


async def _mirror_sell_close(
    db_path: Path,
    trade: dict[str, Any],
) -> int:
    """Close any open paper position that was sourced from this cohort
    wallet on the same (market, outcome). The cohort wallet just sold —
    we exit at their sell price, taking realized P&L on the difference.

    Returns the number of positions closed.
    """
    import aiosqlite

    market_id = str(trade["market_id"])
    outcome = str(trade["outcome"]).upper()
    wallet = str(trade["wallet_address"]).lower()
    exit_price_cents = int(trade["price_cents"])
    market = await dao.get_market(db_path, market_id)

    from polysim.utils.time import iso, now_utc

    closed = 0
    closed_at = iso(now_utc())
    async with aiosqlite.connect(str(db_path)) as conn:
        conn.row_factory = aiosqlite.Row
        # Close, credit proceeds, and record the fill as one transaction.
        # A prior implementation only changed position status, permanently
        # removing the entry collateral from the paper bankroll.
        await conn.execute("BEGIN IMMEDIATE")
        async with conn.execute(
            "SELECT id, run_id, size_shares, avg_entry_price_cents "
            "FROM paper_positions "
            "WHERE status = 'OPEN' AND market_id = ? AND outcome = ? "
            "AND LOWER(COALESCE(source_wallet, '')) = ?",
            (market_id, outcome, wallet),
        ) as cur:
            rows = list(await cur.fetchall())
        for r in rows:
            position_id = int(r["id"])
            run_id = int(r["run_id"])
            size = int(r["size_shares"])
            entry = int(r["avg_entry_price_cents"])
            fee_cents = taker_fee_cents(
                shares=size,
                price_cents=exit_price_cents,
                category=(market.category if market else None),
                metadata=(market.metadata if market else None),
            )
            realized = size * (exit_price_cents - entry) - fee_cents
            proceeds = size * exit_price_cents - fee_cents
            update = await conn.execute(
                "UPDATE paper_positions SET closed_at = ?, "
                "realized_pnl_cents = ?, status = 'CLOSED' "
                "WHERE id = ? AND status = 'OPEN'",
                (closed_at, realized, position_id),
            )
            changed = int(update.rowcount or 0)
            await update.close()
            if changed != 1:
                continue
            await conn.execute(
                "UPDATE paper_runs SET current_balance_cents = "
                "current_balance_cents + ? WHERE id = ?",
                (proceeds, run_id),
            )
            await conn.execute(
                "INSERT INTO paper_fills("
                "run_id, position_id, side, size_shares, fill_price_cents, "
                "intended_price_cents, slippage_cents, latency_ms, fee_cents, "
                "timestamp) VALUES (?, ?, 'SELL', ?, ?, ?, 0, 0, ?, ?)",
                (
                    run_id,
                    position_id,
                    size,
                    exit_price_cents,
                    exit_price_cents,
                    fee_cents,
                    closed_at,
                ),
            )
            closed += 1
        await conn.commit()
    return closed


async def _write_cohort_copy_flag(
    db_path: Path,
    trade: dict[str, Any],
) -> int | None:
    """Insert a high-composite synthetic flag attributed to CohortCopy."""
    from polysim.models import Flag
    from polysim.utils.time import now_utc

    flag = Flag(
        wallet_address=str(trade["wallet_address"]),
        market_id=str(trade["market_id"]),
        trade_id=str(trade["id"]),
        detector_name="CohortCopy",
        raw_score=10.0,
        composite_score=10.0,
        components={
            "weights": {"CohortCopy": 1.0},
            "contribution_by_detector": {"CohortCopy": 10.0},
            "contributing_detectors": ["CohortCopy"],
            "per_detector": {
                "CohortCopy": {
                    "raw_score": 10.0,
                    "confidence": 1.0,
                    "components": {
                        "side": str(trade["side"]),
                        "outcome": str(trade["outcome"]),
                        "size_shares": int(trade["size_shares"]),
                        "price_cents": int(trade["price_cents"]),
                    },
                    "evidence": [
                        "wallet in current cohort",
                        f"notional ${int(trade['size_shares']) * int(trade['price_cents']) / 100:.2f}",
                    ],
                },
            },
        },
        investigator_verdict=None,
        investigator_reasoning=None,
        created_at=now_utc(),
        acted_on=False,
    )
    return await dao.write_flag(db_path, flag)
