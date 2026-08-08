"""EquityTrackLoop — the daily heartbeat of the equity sentiment track.

Each tick: refresh prices -> ingest sentiment -> build daily signals ->
run every variant executor. Seeds one paper_run per variant on first start
(idempotent; reuses an open run across restarts). Tag = 'equity_v1'.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
from typing import Any

from polysim.db import dao
from polysim.equity import prices, sentiment, signal
from polysim.equity.executor import EquityExecutor
from polysim.equity.universe import (
    ALLOWLIST,
    ETF_BENCHMARKS,
    resolve_active_universe,
)
from polysim.equity.variants import select_variants

log = logging.getLogger(__name__)

EQUITY_TAG = "equity_v1"


class EquityTrackLoop:
    def __init__(
        self,
        db_path: Path,
        *,
        interval_s: float = 24 * 3600,
        balance_cents: int = 1_000_000,    # $10k
        first_delay_s: float = 120.0,
        variant_names: list[str] | None = None,
    ) -> None:
        self._db = db_path
        self._interval = interval_s
        self._balance = balance_cents
        self._first_delay = first_delay_s
        self._variants = select_variants(variant_names)
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.run_ids: dict[str, int] = {}
        self.ticks = 0

    async def start(self) -> None:
        self._stop.clear()
        try:
            await self.seed_runs()
        except Exception as exc:
            log.warning("equity: initial strategy reconciliation failed: %s", exc)
        self._task = asyncio.create_task(self._run(), name="equity-track")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        # small delay so startup ingest churn settles, then first tick
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=self._first_delay)
            return
        except TimeoutError:
            pass
        while not self._stop.is_set():
            try:
                await self.tick_once()
            except Exception as exc:
                log.warning("equity tick failed: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
                return
            except TimeoutError:
                continue

    async def seed_runs(self) -> int:
        await self._end_unselected_runs()
        created = 0
        for v in self._variants:
            name = f"equity-{v.name}"
            existing = await dao.find_open_run_by_name(
                self._db, name=name, tag=EQUITY_TAG)
            if existing is not None:
                self.run_ids[v.name] = int(existing["id"])
                continue
            rid = await dao.create_paper_run(
                self._db, name=name, starting_balance_cents=self._balance,
                config_snapshot={"variant": v.name, "kind": v.kind},
                notes=v.description, profile_name=v.name, tag=EQUITY_TAG,
            )
            self.run_ids[v.name] = rid
            created += 1
        if created:
            log.info("equity: seeded %d variant runs", created)
        return created

    async def _end_unselected_runs(self) -> int:
        selected_names = {f"equity-{variant.name}" for variant in self._variants}
        ended = 0
        for run in await dao.list_paper_runs_by_tag(self._db, tag=EQUITY_TAG):
            if run.get("ended_at") is not None or run.get("name") in selected_names:
                continue
            prior_notes = str(run.get("notes") or "").strip()
            note = "strategy_set_v2: retired after performance review"
            if prior_notes:
                note = f"{prior_notes}\n{note}"
            await dao.end_paper_run(self._db, int(run["id"]), notes=note)
            ended += 1
        if ended:
            log.info("equity: ended %d retired strategy runs", ended)
        return ended

    async def tick_once(self) -> dict[str, Any]:
        # 0. resolve the active universe = curated seed + chatter-promoted names
        universe = await resolve_active_universe(self._db)
        # 1. prices for the active universe + benchmarks
        n_px = await prices.refresh_universe(self._db, universe + ETF_BENCHMARKS)
        # 2. sentiment — ApeWisdom across the broad allowlist (enables promotion),
        #    StockTwits only on the active set
        sent = await sentiment.ingest_all(self._db, set(ALLOWLIST.keys()), universe)
        # 3. daily composite signals
        n_sig = await signal.build_daily_signals(self._db, universe)
        # 4. run each variant
        results: dict[str, dict[str, Any]] = {}
        for v in self._variants:
            rid = self.run_ids.get(v.name)
            if rid is None:
                continue
            ex = EquityExecutor(self._db, run_id=rid, variant=v)
            variant_universe = list(v.symbols) if v.symbols else universe
            try:
                results[v.name] = await ex.tick(variant_universe)
            except Exception as exc:
                log.warning("equity variant %s tick failed: %s", v.name, exc)
        self.ticks += 1
        log.info("equity tick: quotes=%d sent=%s signals=%d variants=%d",
                 n_px, sent, n_sig, len(results))
        return {"quotes": n_px, "sentiment": sent, "signals": n_sig,
                "variants": results}
