"""REST-based trade poller.

Polymarket retired the global `/ws/live-activity` firehose; the replacement
public surface for wallet-attributed fills is HTTP polling against
`data-api.polymarket.com/trades`. This module mirrors what
`PolymarketWSIngestor` did — one persistent source of TradeEvents into
every subscribed queue — but uses short-interval polling with a
high-water-mark timestamp cursor so we don't re-ingest the same fills.

On each tick:
  1. GET /trades?limit=N                    (newest-first)
  2. Keep items with timestamp > last_seen  (dedup by (id, timestamp))
  3. Push to every subscribed queue
  4. Advance cursor to newest timestamp seen

Rate: `poll_interval_s` defaults to 5s (Polymarket's trades endpoint is
unauthenticated and tolerates ≤1 rps comfortably).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from polysim.ingest.polymarket_rest import PolymarketREST
from polysim.models import TradeEvent

log = logging.getLogger(__name__)


class PolymarketTradesPoller:
    """Polls data-api /trades → emits TradeEvents to subscribed queues."""

    def __init__(
        self,
        rest: PolymarketREST,
        *,
        poll_interval_s: float = 5.0,
        page_size: int = 250,
        max_backoff_s: float = 60.0,
    ) -> None:
        self._rest = rest
        self._poll = poll_interval_s
        self._page_size = page_size
        self._max_backoff_s = max_backoff_s
        self._queues: list[asyncio.Queue[TradeEvent]] = []
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._seen_ids: set[str] = set()
        self._latest_ts: float = 0.0
        self.frame_count = 0  # tick count — mirrors ws ingestor's frame_count
        self.inserted_count = 0

    # ── public API ───────────────────────────────────────

    def subscribe(self, queue: asyncio.Queue[TradeEvent]) -> None:
        self._queues.append(queue)

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="polymarket-trades-poll")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._task, timeout=5.0)
            if not self._task.done():
                self._task.cancel()
                with contextlib.suppress(BaseException):
                    await self._task
            self._task = None

    def drain_clock_skews_ms(self) -> list[int]:
        """Polling doesn't carry a server timestamp we can diff against; no skew samples."""
        return []

    # ── loop ─────────────────────────────────────────────

    async def _run(self) -> None:
        backoff = 1.0
        while not self._stop_event.is_set():
            try:
                n = await self._tick_once()
                self.frame_count += 1
                if n > 0:
                    backoff = 1.0
                    log.debug("trade poll: +%d trades (cursor=%.0f)", n, self._latest_ts)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Rate-limit, transient network, etc. — back off and keep polling.
                if self._stop_event.is_set():
                    return
                log.warning("trade poll error: %s — backing off %.1fs", exc, backoff)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop_event.wait(), timeout=backoff)
                if self._stop_event.is_set():
                    return
                backoff = min(backoff * 2, self._max_backoff_s)
                continue

            # Sleep between ticks (interruptible).
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._poll)
                return  # stop requested
            except TimeoutError:
                continue

    async def _tick_once(self) -> int:
        """One fetch + dispatch. Returns count of fresh trades pushed."""
        trades = await self._rest.get_trades(limit=self._page_size)
        if not trades:
            return 0

        # Data-api returns newest-first. Keep anything past the cursor and
        # not-yet-seen (id dedup is belt-and-braces).
        fresh: list[TradeEvent] = []
        newest_ts = self._latest_ts
        for t in trades:
            ts = t.timestamp.timestamp()
            if ts < self._latest_ts:
                break  # list is newest-first; we've caught up
            if t.id in self._seen_ids:
                continue
            fresh.append(t)
            if ts > newest_ts:
                newest_ts = ts
        self._latest_ts = newest_ts
        # Cap the seen-ids ring to ~10k entries to bound memory.
        for t in fresh:
            self._seen_ids.add(t.id)
        if len(self._seen_ids) > 10_000:
            # Trim oldest half — order doesn't matter, we've advanced the
            # timestamp cursor anyway.
            self._seen_ids = set(list(self._seen_ids)[-5_000:])

        for ev in fresh:
            for q in self._queues:
                try:
                    q.put_nowait(ev)
                except asyncio.QueueFull:
                    log.warning("trades queue full — dropping %s", ev.id)
        self.inserted_count += len(fresh)
        return len(fresh)


def _asset_id_to_outcome(outcome_index: Any) -> str | None:
    """outcomeIndex 0 → YES, 1 → NO. Multi-outcome markets unsupported here."""
    if outcome_index in (0, "0"):
        return "YES"
    if outcome_index in (1, "1"):
        return "NO"
    return None


__all__ = ["PolymarketTradesPoller", "_asset_id_to_outcome"]
