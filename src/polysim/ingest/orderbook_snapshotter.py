"""Live orderbook snapshotter — build plan §4.8 / spec gap G8.

Periodically polls `PolymarketREST.get_orderbook()` for a watchlist of
markets and writes the result to `orderbook_snapshots`. The fill model
reads the nearest snapshot at execution time; when absent, it falls back
to the 2x-pessimism synthetic book (see fill_model.py).

For each watched market we track both the YES token and the NO token —
the Polymarket API returns one book per token_id, and we don't know
which token the insider traded until their trade arrives.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from polysim.db import dao
from polysim.ingest.polymarket_rest import PolymarketREST

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class WatchedToken:
    market_id: str
    outcome: str              # "YES" | "NO"
    token_id: str             # Polymarket CLOB token id


class OrderbookSnapshotter:
    def __init__(
        self,
        db_path: Path,
        rest: PolymarketREST,
        *,
        interval_s: int = 30,
        max_watched: int = 200,
    ) -> None:
        self._db = db_path
        self._rest = rest
        self._interval_s = interval_s
        self._max_watched = max_watched
        self._watchlist: list[WatchedToken] = []
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.snapshots_written = 0

    def set_watchlist(self, tokens: Iterable[WatchedToken]) -> None:
        self._watchlist = list(tokens)[: self._max_watched]

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="orderbook-snapshotter")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def tick_once(self) -> int:
        """Fetch the book for every watched token once. Returns count written."""
        written = 0
        for token in self._watchlist:
            try:
                book = await self._rest.get_orderbook(token.token_id)
            except Exception as exc:
                log.warning(
                    "orderbook fetch failed for %s/%s: %s",
                    token.market_id, token.outcome, exc,
                )
                continue
            asks = _normalize_book_side(book.get("asks") or [])
            bids = _normalize_book_side(book.get("bids") or [])
            if not asks and not bids:
                continue
            await dao.write_orderbook_snapshot(
                self._db,
                market_id=token.market_id,
                outcome=token.outcome,
                asks=asks,
                bids=bids,
            )
            written += 1
        self.snapshots_written += written
        return written

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.tick_once()
            except Exception as exc:
                log.warning("snapshotter tick failed: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval_s)
                return
            except TimeoutError:
                continue


# ── helpers ──────────────────────────────────────────────


def _normalize_book_side(levels: object) -> list[dict[str, int]]:
    """Coerce a book side (list of {price, size}) into our int-cents schema."""
    out: list[dict[str, int]] = []
    if not isinstance(levels, list):
        return out
    for lvl in levels:
        if not isinstance(lvl, dict):
            continue
        price = lvl.get("price") or lvl.get("price_cents")
        size = lvl.get("size") or lvl.get("size_shares")
        price_cents = _to_price_cents(price)
        size_shares = _to_size_shares(size)
        if price_cents is None or size_shares is None or size_shares <= 0:
            continue
        out.append({"price_cents": price_cents, "size_shares": size_shares})
    return out


def _to_price_cents(v: object) -> int | None:
    try:
        f = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if 0.0 <= f <= 1.0:
        return round(f * 100)
    if 0 <= f <= 100 and abs(f - round(f)) < 1e-9:
        return round(f)
    return None


def _to_size_shares(v: object) -> int | None:
    try:
        f = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if f < 0:
        return None
    return round(f)
