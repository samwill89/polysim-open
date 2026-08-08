"""Uptime watchdog — build plan §6.5.

Polls the most recent `trades.timestamp` and raises an alert when the
gap from now exceeds `stall_threshold_s`. Hysteresis: once the stall
alarm fires, it won't re-fire more than once per `rearm_s` seconds
while the stall persists.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from pathlib import Path

import aiosqlite

from polysim.utils.time import now_utc

log = logging.getLogger(__name__)


AlertFn = Callable[[str], Awaitable[None]]


async def latest_trade_timestamp(
    db_path: Path,
) -> datetime | None:
    """Return the newest trades.timestamp or None when the table is empty."""
    if not db_path.exists():
        return None
    try:
        async with (
            aiosqlite.connect(str(db_path)) as db,
            db.execute(
                "SELECT timestamp FROM trades ORDER BY timestamp DESC LIMIT 1"
            ) as cur,
        ):
            row = await cur.fetchone()
    except aiosqlite.OperationalError:
        return None
    if row is None:
        return None
    from polysim.utils.time import parse_iso
    try:
        return parse_iso(str(row[0]))
    except ValueError:
        return None


def stall_seconds(
    last_trade_at: datetime | None, now: datetime
) -> float | None:
    """Seconds since last trade, or None if we have nothing to compare."""
    if last_trade_at is None:
        return None
    return (now - last_trade_at).total_seconds()


class UptimeWatchdog:
    def __init__(
        self,
        db_path: Path,
        *,
        alert_fn: AlertFn,
        stall_threshold_s: int = 300,
        check_interval_s: int = 60,
        rearm_s: int = 3600,
    ) -> None:
        self._db = db_path
        self._alert_fn = alert_fn
        self._stall_threshold = stall_threshold_s
        self._check_interval = check_interval_s
        self._rearm = timedelta(seconds=rearm_s)
        self._last_alert_at: datetime | None = None
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.fires = 0  # test hook

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="uptime-watchdog")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def check_once(self) -> bool:
        """Run a single check. Returns True iff an alert was emitted."""
        last = await latest_trade_timestamp(self._db)
        gap = stall_seconds(last, now_utc())
        if gap is None or gap <= self._stall_threshold:
            return False
        # Hysteresis
        now = now_utc()
        if self._last_alert_at and (now - self._last_alert_at) < self._rearm:
            return False
        minutes = int(gap / 60)
        msg = f"Ingest stalled: last trade {minutes}m ago"
        try:
            await self._alert_fn(msg)
        except Exception as exc:
            log.warning("watchdog alert_fn raised: %s", type(exc).__name__)
            return False
        self._last_alert_at = now
        self.fires += 1
        return True

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.check_once()
            except Exception as exc:
                log.warning("watchdog check failed: %s", type(exc).__name__)
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self._check_interval
                )
                return
            except TimeoutError:
                continue
