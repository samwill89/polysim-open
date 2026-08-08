"""Backtest orchestration — replay-driven scoring + paper run.

Build plan §1 (replay) + §5.4 (run_backtest).

Two entry points:
  * `replay_window`     — pure scoring replay over a time window. For each
                          trade in [start, end] hits the profiler+scorer+
                          composite. Does NOT open paper positions.
  * `run_backtest`      — full pipeline: replay window + start a paper run,
                          dispatch flags through executor as they fire.

Both are deterministic given a fixed DB + config + seed.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

import aiosqlite

from polysim.config import Config, load_config
from polysim.db import dao
from polysim.models import TradeEvent
from polysim.profiler.wallet_profiler import refresh_stale_profiles
from polysim.scoring.composite import CompositeScorer, score_and_persist
from polysim.utils.time import iso, parse_iso

log = logging.getLogger(__name__)


async def _iter_trades_in_window(
    db_path: Path, *, start: datetime, end: datetime, limit: int = 100_000
) -> list[dict[str, Any]]:
    """Stream trades in [start, end] joined with markets — caller decides batching."""
    if not db_path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        async with aiosqlite.connect(str(db_path)) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT id, wallet_address, market_id, side, outcome,
                       size_shares, price_cents, timestamp
                FROM trades
                WHERE timestamp >= ? AND timestamp <= ?
                ORDER BY timestamp ASC LIMIT ?
                """,
                (iso(start), iso(end), limit),
            ) as cur:
                rows = [dict(r) for r in await cur.fetchall()]
    except aiosqlite.OperationalError:
        return []
    return rows


async def _build_detectors(db_path: Path, cfg: Config) -> list[object]:
    from polysim.scoring.category_insider import CategoryInsiderDetector
    from polysim.scoring.coordination import CoordinationDetector
    from polysim.scoring.event_insider import EventInsiderDetector
    from polysim.scoring.fresh_wallet import FreshWalletDetector
    from polysim.scoring.timing import TimingDetector

    # G5: load per-category late-activity base rates from `meta` if present.
    meta_baselines = await dao.meta_get_json(db_path, key="timing_baselines")
    base_rates: dict[str, float] = {}
    if isinstance(meta_baselines, dict):
        for k, v in meta_baselines.items():
            try:
                base_rates[str(k)] = float(v)
            except (TypeError, ValueError):
                continue

    return [
        CategoryInsiderDetector(
            min_resolved_markets=cfg.detectors.category_insider.min_resolved_markets,
        ),
        EventInsiderDetector(
            fresh_nonce_threshold=cfg.detectors.event_insider.fresh_nonce_threshold,
            fresh_size_min_cents=cfg.detectors.event_insider.fresh_size_min_cents,
            niche_market_vol_max_cents=cfg.detectors.event_insider.niche_market_vol_max_cents,
            contrarian_bps=cfg.detectors.event_insider.contrarian_bps,
        ),
        FreshWalletDetector(),
        CoordinationDetector(
            db_path,
            window_hours=cfg.detectors.coordination.window_hours,
        ),
        TimingDetector(
            db_path,
            late_window_hours=cfg.detectors.timing.late_window_hours,
            base_rates_by_category=base_rates or None,
        ),
    ]


def _build_scorer(cfg: Config) -> CompositeScorer:
    return CompositeScorer(
        flag_threshold=cfg.scoring.flag_threshold,
        min_contributing_detectors=cfg.scoring.min_contributing_detectors,
        weights={
            "CategoryInsiderDetector": cfg.scoring.weights.CategoryInsiderDetector,
            "EventInsiderDetector": cfg.scoring.weights.EventInsiderDetector,
            "TimingDetector": cfg.scoring.weights.TimingDetector,
            "CoordinationDetector": cfg.scoring.weights.CoordinationDetector,
            "FreshWalletDetector": cfg.scoring.weights.FreshWalletDetector,
        },
    )


async def replay_window(
    db_path: Path,
    *,
    start: datetime,
    end: datetime,
    cfg: Config | None = None,
    refresh_profiles_first: bool = True,
    progress_every: int = 1000,
) -> dict[str, int]:
    """Replay every trade in [start, end] through profiler+scorer.

    Returns counts: {"trades_seen", "profiles_refreshed", "flags_created"}.
    Idempotent w.r.t. flags — relies on the scorer's dedup window.
    """
    cfg = cfg or load_config()
    detectors = await _build_detectors(db_path, cfg)
    scorer = _build_scorer(cfg)

    profiles_refreshed = 0
    if refresh_profiles_first:
        # Touch every wallet that traded in the window so detectors see fresh
        # profiles when they score.
        profiles_refreshed = await refresh_stale_profiles(
            db_path, staleness_seconds=0, max_wallets=10_000
        )

    trades = await _iter_trades_in_window(db_path, start=start, end=end)
    flags_created = 0
    for i, t in enumerate(trades, start=1):
        try:
            new_id = await score_and_persist(
                db_path,
                scorer,
                detectors,
                wallet_address=str(t["wallet_address"]),
                market_id=str(t["market_id"]),
                trade_id=str(t["id"]),
                investigator=None,  # backtest stays offline; investigator can be re-run via CLI
                min_composite_to_invoke=cfg.investigator.min_composite_to_invoke,
            )
            if new_id is not None:
                flags_created += 1
        except Exception as exc:
            log.warning("score_and_persist failed for trade %s: %s", t.get("id"), exc)
        if progress_every and i % progress_every == 0:
            log.info(
                "replay progress: %d/%d trades, %d flags",
                i, len(trades), flags_created,
            )

    return {
        "trades_seen": len(trades),
        "profiles_refreshed": profiles_refreshed,
        "flags_created": flags_created,
    }


async def run_backtest(
    start: datetime,
    end: datetime,
    *,
    db_path: Path = Path("./polysim.db"),
    cfg: Config | None = None,
    open_paper_run: bool = True,
    profile_name: str = "systematic",
    speed_multiplier: float = 100.0,  # accepted for CLI compat; replay is offline
) -> dict[str, Any]:
    """Full backtest: replay → flags → optional paper run + executor pass.

    Returns a dict with run_id (if open_paper_run=True), flag count, and
    open positions count.
    """
    _ = speed_multiplier  # not used by the offline replay
    cfg = cfg or load_config()

    rep = await replay_window(db_path, start=start, end=end, cfg=cfg)

    out: dict[str, Any] = {**rep, "run_id": None, "positions_opened": 0}

    if not open_paper_run or rep["flags_created"] == 0:
        return out

    # Build a profile-aware run + sweep the flags through it.
    from polysim.paper.fill_model import FillModel
    from polysim.paper.profile_executor import Dispatcher, ProfilePaperExecutor
    from polysim.paper.run_manager import start_run
    from polysim.profiles import load_profile

    profile = load_profile(profile_name)
    run_id = await start_run(
        db_path, cfg,
        profile=profile,
        name_override=f"backtest-{profile.name}-{start.date()}",
        balance_override_cents=cfg.run.starting_balance_cents,
        tag=f"backtest-{start.date().isoformat()}-{end.date().isoformat()}",
    )
    out["run_id"] = run_id

    fill_model = FillModel(
        detection_latency_p50_ms=cfg.fill_model.detection_latency_p50_ms,
        detection_latency_p95_ms=cfg.fill_model.detection_latency_p95_ms,
        decision_latency_p50_ms=cfg.fill_model.decision_latency_p50_ms,
        decision_latency_p95_ms=cfg.fill_model.decision_latency_p95_ms,
        slippage_ticks=cfg.fill_model.slippage_ticks,
        on_partial=cfg.fill_model.on_partial,
        fee_bps=cfg.fill_model.fee_bps,
        historical_pessimism_multiplier=cfg.fill_model.historical_pessimism_multiplier,
    )
    executor = ProfilePaperExecutor(
        db_path,
        run_id=run_id,
        profile=profile,
        bankroll=cfg.bankroll,
        fill_model=fill_model,
    )
    dispatcher = Dispatcher([executor])

    # Walk flags created in our window in chronological order.
    flag_ids = await _flag_ids_in_window(db_path, start=start, end=end)
    opened = 0
    for fid in flag_ids:
        result = await dispatcher.on_flag(fid)
        if result.get(run_id) is not None:
            opened += 1
    out["positions_opened"] = opened
    return out


async def _flag_ids_in_window(
    db_path: Path, *, start: datetime, end: datetime
) -> list[int]:
    if not db_path.exists():
        return []
    try:
        async with aiosqlite.connect(str(db_path)) as db, db.execute(
            "SELECT id FROM flags WHERE created_at >= ? AND created_at <= ? "
            "ORDER BY id ASC",
            (iso(start), iso(end)),
        ) as cur:
            rows = await cur.fetchall()
    except aiosqlite.OperationalError:
        return []
    return [int(r[0]) for r in rows]


def parse_speed(s: str) -> float:
    """Accept "100x" / "10x" / "1x" / "0.5x" — returns the multiplier."""
    s = s.strip().lower().rstrip("x")
    try:
        return float(s)
    except ValueError as exc:
        raise ValueError(f"bad --speed value: {s!r} (expected '100x', '1x', etc.)") from exc


# Re-exports for tests/CLI.
__all__ = [
    "_iter_trades_in_window",
    "parse_speed",
    "replay_window",
    "run_backtest",
]


# Suppress unused-import warning for typing (used in the public API).
_ = (Iterable, TradeEvent, parse_iso, asyncio)
