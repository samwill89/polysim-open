"""Daily summary scheduler — build plan §6.4.

Fires once per day at the configured local hour. Computes a
`DailySummaryAlert` from DB state and pushes it to the provided
`AlertSink`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from polysim.db import dao
from polysim.reporter.telegram import AlertSink, DailySummaryAlert
from polysim.utils.time import iso, now_utc

log = logging.getLogger(__name__)


SummaryProvider = Callable[[], Awaitable[DailySummaryAlert]]


def next_fire_time(
    *,
    now: datetime,
    fire_hour_local: int,
    tz: ZoneInfo,
) -> datetime:
    """Return the next datetime at `fire_hour_local:00` in timezone `tz`
    that is strictly in the future relative to `now` (converted to `tz`).
    """
    now_local = now.astimezone(tz)
    target = datetime.combine(
        now_local.date(), time(hour=fire_hour_local), tzinfo=tz
    )
    if target <= now_local:
        target = target + timedelta(days=1)
    return target


async def default_summary_provider(db_path: Path) -> DailySummaryAlert:
    """Default implementation — aggregates active paper_runs + flag_costs."""
    import json

    runs_rows = await dao.list_active_runs(db_path)
    run_summaries: list[dict[str, object]] = []
    top_cat: tuple[str, int] | None = None
    worst_cat: tuple[str, int] | None = None
    for row in runs_rows:
        rid = int(row["id"])
        summary = await dao.get_paper_run(db_path, rid)
        if summary is None:
            continue
        start = int(summary.get("starting_balance_cents") or 0)
        current = int(summary.get("current_balance_cents") or 0)
        pnl = current - start
        pct = (pnl / start) if start else 0.0
        # flagged + acted counts over last 24h
        since = iso(now_utc() - timedelta(hours=24))
        flags = await dao.list_flags_since(
            db_path, since_iso=since, category=None, limit=1000
        )
        flagged = 0
        acted = 0
        for f in flags:
            flagged += 1
            if f.get("acted_on"):
                acted += 1
        # win rate from closed-today positions in this run (best-effort).
        wins = losses = 0
        import aiosqlite

        async with aiosqlite.connect(str(db_path)) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT realized_pnl_cents FROM paper_positions "
                "WHERE run_id = ? AND status IN ('CLOSED','RESOLVED') "
                "AND closed_at >= ?",
                (rid, since),
            ) as cur:
                for r in await cur.fetchall():
                    p = int(r["realized_pnl_cents"] or 0)
                    if p > 0:
                        wins += 1
                    elif p < 0:
                        losses += 1
        total = wins + losses
        win_rate = wins / total if total else 0.0
        run_summaries.append(
            {
                "id": rid,
                "name": summary.get("name"),
                "pnl_cents": pnl,
                "pct": pct,
                "win_rate": win_rate,
                "acted": acted,
                "flagged": flagged,
            }
        )

        # Crude top/worst category aggregation across active runs.
        # Category attribution uses position->market->category.
        pos_rows = await dao.list_open_positions(db_path, rid)
        cat_totals: dict[str, int] = {}
        for pos_row in pos_rows:
            m = await dao.get_market(db_path, str(pos_row["market_id"]))
            if m is None or m.category is None:
                continue
            cat_totals[m.category] = cat_totals.get(m.category, 0) + int(
                (pos_row.get("size_shares") or 0)
                * (pos_row.get("avg_entry_price_cents") or 0)
            )
        if cat_totals:
            best = max(cat_totals.items(), key=lambda kv: kv[1])
            worst = min(cat_totals.items(), key=lambda kv: kv[1])
            top_cat = best if top_cat is None or best[1] > top_cat[1] else top_cat
            worst_cat = worst if worst_cat is None or worst[1] < worst_cat[1] else worst_cat

    # Cost rollup last 24h.
    cost_totals = await dao.sum_flag_costs_since(
        db_path, since_iso=iso(now_utc() - timedelta(hours=24))
    )

    _ = json  # keep import tidy for future serialization needs
    return DailySummaryAlert(
        as_of_iso=now_utc().date().isoformat(),
        runs=run_summaries,
        top_category=top_cat,
        worst_category=worst_cat,
        llm_calls_today=cost_totals.get("total_calls", 0),
        llm_cost_cents_today=cost_totals.get("total_cost_cents", 0),
        kill_switches_nominal=not any(
            bool(r.get("paused_at")) for r in runs_rows
        ),
    )


class DailySummaryScheduler:
    """Fires a daily summary at configured local hour until stopped."""

    def __init__(
        self,
        sink: AlertSink,
        *,
        provider: SummaryProvider,
        fire_hour_local: int = 9,
        tz_name: str = "UTC",
    ) -> None:
        if not (0 <= fire_hour_local <= 23):
            raise ValueError("fire_hour_local must be 0..23")
        self._sink = sink
        self._provider = provider
        self._fire_hour = fire_hour_local
        self._tz = ZoneInfo(tz_name)
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="daily-summary")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            target = next_fire_time(
                now=now_utc(),
                fire_hour_local=self._fire_hour,
                tz=self._tz,
            )
            delay = (target - now_utc().astimezone(self._tz)).total_seconds()
            if delay <= 0:
                delay = 60  # safety — never busy-loop
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
                return
            except TimeoutError:
                pass
            try:
                alert = await self._provider()
                await self._sink.send_daily_summary(alert)
            except Exception as exc:
                log.warning("daily summary failed: %s", type(exc).__name__)
