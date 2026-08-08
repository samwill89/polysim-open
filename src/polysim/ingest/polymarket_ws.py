"""Polymarket CLOB WebSocket ingestor.

Spec §7.1, build plan §1.1 (reconnect) + §1.9 (clock skew, G3).

Architecture:
  - One persistent WS connection, exponential-backoff reconnect with jitter.
  - Per-frame: parse → emit TradeEvents to every subscribed queue.
  - Every Nth frame sample clock skew (WS server timestamp vs local clock).
  - `parse_frame` is a pure function (module-level) so tests can exercise
    parsing independently of the network.

The wire format Polymarket uses has shifted over time. We parse defensively
via `ingest.parsing`; unknown frame shapes are logged at DEBUG and skipped.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
from datetime import UTC, datetime
from typing import Any

import websockets

from polysim.ingest.parsing import parse_timestamp, parse_trade
from polysim.models import TradeEvent

log = logging.getLogger(__name__)

DEFAULT_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/live-activity"
DEFAULT_SUBSCRIBE_MESSAGE: dict[str, Any] = {
    "type": "subscribe",
    "channels": ["trades"],
}


class PolymarketWSIngestor:
    """Streams Polymarket fills to every subscribed asyncio.Queue[TradeEvent]."""

    def __init__(
        self,
        *,
        ws_url: str = DEFAULT_WS_URL,
        subscribe_message: dict[str, Any] | None = None,
        max_backoff_s: float = 60.0,
        skew_sample_every_n_frames: int = 500,
    ) -> None:
        self._ws_url = ws_url
        self._subscribe_message = (
            subscribe_message if subscribe_message is not None else dict(DEFAULT_SUBSCRIBE_MESSAGE)
        )
        self._max_backoff_s = max_backoff_s
        self._skew_sample_every_n_frames = skew_sample_every_n_frames
        self._queues: list[asyncio.Queue[TradeEvent]] = []
        self._skew_samples_ms: list[int] = []
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._frame_count = 0

    # ── public API ────────────────────────────────────────
    def subscribe(self, queue: asyncio.Queue[TradeEvent]) -> None:
        self._queues.append(queue)

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="polymarket-ws")

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
        """Return accumulated skew samples and reset. Called by the DAO writer."""
        out = list(self._skew_samples_ms)
        self._skew_samples_ms.clear()
        return out

    @property
    def frame_count(self) -> int:
        return self._frame_count

    # ── connection management ─────────────────────────────
    async def _run(self) -> None:
        backoff = 1.0
        while not self._stop_event.is_set():
            try:
                async with websockets.connect(self._ws_url) as ws:
                    log.info("ws connected: %s", self._ws_url)
                    await ws.send(json.dumps(self._subscribe_message))
                    backoff = 1.0  # reset backoff after successful connect
                    await self._pump(ws)
            except asyncio.CancelledError:
                raise
            except (websockets.WebSocketException, OSError) as exc:
                if self._stop_event.is_set():
                    return
                jittered = backoff + random.uniform(0, backoff)
                log.warning("ws error: %s — reconnecting in %.1fs", exc, jittered)
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=jittered)
                    return  # stop requested during backoff
                except TimeoutError:
                    pass
                backoff = min(backoff * 2, self._max_backoff_s)

    async def _pump(self, ws: Any) -> None:
        stop_waiter = asyncio.create_task(self._stop_event.wait(), name="ws-stop-waiter")
        try:
            while not self._stop_event.is_set():
                recv_task = asyncio.create_task(ws.recv())
                done, _ = await asyncio.wait(
                    {recv_task, stop_waiter},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if stop_waiter in done:
                    recv_task.cancel()
                    with contextlib.suppress(BaseException):
                        await recv_task
                    return
                try:
                    frame = recv_task.result()
                except websockets.ConnectionClosed:
                    return
                await self._handle_frame(frame)
        finally:
            stop_waiter.cancel()
            with contextlib.suppress(BaseException):
                await stop_waiter

    async def _handle_frame(self, raw: str | bytes) -> None:
        self._frame_count += 1
        events, server_ts = parse_frame(raw)
        if (
            server_ts is not None
            and self._frame_count % self._skew_sample_every_n_frames == 0
        ):
            skew_ms = int((datetime.now(UTC) - server_ts).total_seconds() * 1000)
            self._skew_samples_ms.append(skew_ms)
        for ev in events:
            for q in self._queues:
                try:
                    q.put_nowait(ev)
                except asyncio.QueueFull:
                    log.warning("ws queue full — dropping trade %s", ev.id)


# ── pure parser — testable without a live socket ──────────


def parse_frame(raw: str | bytes) -> tuple[list[TradeEvent], datetime | None]:
    """Parse one WS frame. Returns ``(trades, server_timestamp_or_None)``.

    Defensive: returns ``([], None)`` on any parse failure rather than
    raising. Handles object frames, array-of-objects batches, and
    Polymarket's common envelope shapes (``{"type":"trade","data":{...}}``
    or ``{"event_type":"trade", ...}``).
    """
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            return [], None
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return [], None

    items = _flatten_frame(obj)
    trades: list[TradeEvent] = []
    server_ts: datetime | None = None
    for item in items:
        # Server-timestamp hint — some frames carry one even when there's no trade.
        ts = parse_timestamp(
            item.get("server_time") or item.get("serverTime") or item.get("timestamp")
        )
        if ts is not None and server_ts is None:
            server_ts = ts
        if _looks_like_trade(item):
            ev = parse_trade(item)
            if ev is not None:
                trades.append(ev)
    return trades, server_ts


def _flatten_frame(obj: Any) -> list[dict[str, Any]]:
    """Return a list of candidate trade-shaped dicts from one frame."""
    if isinstance(obj, list):
        return [x for x in obj if isinstance(x, dict)]
    if not isinstance(obj, dict):
        return []

    # Envelope: {"type":"trade","data":{...}} or {"type":"trade","events":[...]}
    if isinstance(obj.get("data"), dict):
        inner = obj["data"]
        return [inner]
    if isinstance(obj.get("data"), list):
        return [x for x in obj["data"] if isinstance(x, dict)]
    if isinstance(obj.get("events"), list):
        return [x for x in obj["events"] if isinstance(x, dict)]
    return [obj]


_TRADE_TYPE_TOKENS = frozenset(
    {"trade", "match", "fill", "trades", "matches", "fills"}
)


def _looks_like_trade(item: dict[str, Any]) -> bool:
    for key in ("event_type", "type", "eventType"):
        val = item.get(key)
        if isinstance(val, str) and val.lower() in _TRADE_TYPE_TOKENS:
            return True
    # Duck-type: price + size + side is trade-shaped.
    return "price" in item and ("size" in item or "shares" in item) and "side" in item
