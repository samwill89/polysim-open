"""Ingest pipeline orchestrator.

Wires together WS + REST + Polygon enricher + category classifier + DAO
persistence. Used by `polysim ingest start` and `ingest backfill`.

Topology (one process, pure asyncio, backpressure via bounded queues):

    PolymarketWSIngestor ─── trade_queue ─┐
                                          ├──▶ TradeWriter ──▶ SQLite
                                          └──▶ wallet_sight ──▶ Enricher
    hourly tick ──▶ MarketIndexer ──▶ Classifier ──▶ SQLite
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path

from polysim.config import Config, Secrets
from polysim.db import dao
from polysim.ingest.category import Classifier
from polysim.ingest.funding_sources import FundingSources
from polysim.ingest.polygon_rpc import PolygonEnricher, safe_rpc_error
from polysim.ingest.polymarket_rest import PolymarketREST
from polysim.ingest.polymarket_trades_poller import PolymarketTradesPoller
from polysim.models import TradeEvent
from polysim.utils.time import iso

log = logging.getLogger(__name__)


class TradeWriter:
    """Drains the trade queue in batches and writes to SQLite."""

    def __init__(
        self,
        db_path: Path,
        trade_queue: asyncio.Queue[TradeEvent],
        *,
        batch_size: int = 50,
        max_latency_s: float = 2.0,
    ) -> None:
        self._db = db_path
        self._q = trade_queue
        self._batch_size = batch_size
        self._max_latency_s = max_latency_s
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._new_wallet_queue: asyncio.Queue[str] | None = None
        self.inserted_count = 0

    def set_wallet_sight_queue(self, q: asyncio.Queue[str]) -> None:
        self._new_wallet_queue = q

    async def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="trade-writer")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            batch = await self._collect_batch()
            if not batch:
                continue
            try:
                n = await dao.insert_trades_batch(self._db, batch)
                self.inserted_count += n
            except Exception as exc:
                log.exception("trade batch insert failed: %s", exc)
                continue
            # Queue new wallets for enrichment.
            if self._new_wallet_queue is not None:
                seen: set[str] = set()
                for t in batch:
                    w = t.wallet_address.lower()
                    if w in seen:
                        continue
                    seen.add(w)
                    with contextlib.suppress(asyncio.QueueFull):
                        self._new_wallet_queue.put_nowait(w)

    async def _collect_batch(self) -> list[TradeEvent]:
        batch: list[TradeEvent] = []
        try:
            first = await asyncio.wait_for(self._q.get(), timeout=self._max_latency_s)
        except TimeoutError:
            return []
        batch.append(first)
        while len(batch) < self._batch_size:
            try:
                batch.append(self._q.get_nowait())
            except asyncio.QueueEmpty:
                break
        return batch


class WalletEnricherWorker:
    """Consumes new-wallet addresses, calls Alchemy, writes back to SQLite."""

    def __init__(
        self,
        db_path: Path,
        enricher: PolygonEnricher,
        wallet_queue: asyncio.Queue[str],
        rest: PolymarketREST | None = None,
    ) -> None:
        self._db = db_path
        self._enricher = enricher
        self._q = wallet_queue
        self._rest = rest
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.enriched_count = 0

    async def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="wallet-enricher")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(BaseException):
                await self._task
            self._task = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                address = await asyncio.wait_for(self._q.get(), timeout=1.0)
            except TimeoutError:
                continue
            try:
                owner = (
                    await self._rest.resolve_proxy_owner(address) if self._rest else None
                )
                e = await self._enricher.enrich(address)
                deposit_iso = iso(e.funding_first_deposit_at) if e.funding_first_deposit_at else None
                await dao.upsert_wallet_enrichment(
                    self._db,
                    address=address,
                    nonce=e.nonce,
                    funding_source=e.funding_source,
                    funding_first_deposit_at=deposit_iso,
                    owner_address=owner,
                )
                self.enriched_count += 1
            except Exception as exc:
                log.warning("enrich(%s) failed: %s", address, safe_rpc_error(exc))


class MarketIndexer:
    """Periodic: pull newly-active markets, classify, upsert."""

    def __init__(
        self,
        db_path: Path,
        rest: PolymarketREST,
        classifier: Classifier,
        *,
        interval_s: int = 3600,
        page_size: int = 100,
        max_pages_per_tick: int = 5,
    ) -> None:
        self._db = db_path
        self._rest = rest
        self._classifier = classifier
        self._interval_s = interval_s
        self._page_size = page_size
        self._max_pages_per_tick = max_pages_per_tick
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.indexed_count = 0

    async def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="market-indexer")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def tick_once(self) -> int:
        """Run one indexing pass. Returns count of markets upserted."""
        n = 0
        async for market in self._rest.iter_markets(
            active=True, page_size=self._page_size, max_pages=self._max_pages_per_tick
        ):
            category = await self._classifier.classify(market.question)
            m = market.model_copy(update={"category": category})
            await dao.upsert_market(self._db, m)
            n += 1
        self.indexed_count += n
        return n

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.tick_once()
            except Exception as exc:
                log.warning("market indexer tick failed: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval_s)
                return
            except TimeoutError:
                continue


class IngestPipeline:
    """Start / stop the whole ingest stack as a single unit."""

    def __init__(
        self,
        *,
        db_path: Path,
        config: Config,
        secrets: Secrets,
    ) -> None:
        self._db = db_path
        self._config = config
        self._secrets = secrets

        funding = FundingSources.load(config.ingest.funding_sources_path)
        classifier = Classifier(config.categories)

        self._rest = PolymarketREST(
            gamma_base_url=config.ingest.polymarket_gamma_url,
            data_base_url=config.ingest.polymarket_data_url,
            clob_base_url=config.ingest.polymarket_clob_url,
        )
        self._enricher = PolygonEnricher(
            api_key=secrets.ALCHEMY_API_KEY,
            funding_sources=funding,
            base_url_template=config.ingest.polygon_rpc_url,
            rps_cap=config.ingest.polygon_rps_cap,
        )
        # Polymarket retired the global live-activity WS; use the public
        # data-api /trades endpoint via short-interval polling instead.
        self._ws = PolymarketTradesPoller(rest=self._rest, poll_interval_s=5.0)
        self._trade_queue: asyncio.Queue[TradeEvent] = asyncio.Queue(maxsize=10_000)
        self._wallet_queue: asyncio.Queue[str] = asyncio.Queue(maxsize=10_000)

        self._ws.subscribe(self._trade_queue)
        self._writer = TradeWriter(
            db_path,
            self._trade_queue,
            batch_size=config.ingest.trade_persist_batch_size,
            max_latency_s=config.ingest.trade_persist_max_latency_s,
        )
        self._writer.set_wallet_sight_queue(self._wallet_queue)

        self._enricher_worker = WalletEnricherWorker(
            db_path, self._enricher, self._wallet_queue, rest=self._rest
        )
        self._indexer = MarketIndexer(
            db_path,
            self._rest,
            classifier,
            interval_s=config.ingest.market_indexer_interval_s,
        )
        self._skew_drain_task: asyncio.Task[None] | None = None

    @property
    def writer(self) -> TradeWriter:
        return self._writer

    @property
    def indexer(self) -> MarketIndexer:
        return self._indexer

    @property
    def enricher_worker(self) -> WalletEnricherWorker:
        return self._enricher_worker

    @property
    def ws(self) -> PolymarketTradesPoller:
        return self._ws

    async def start(self) -> None:
        await self._writer.start()
        await self._enricher_worker.start()
        await self._indexer.start()
        await self._ws.start()
        self._skew_drain_task = asyncio.create_task(
            self._drain_skew_periodically(), name="skew-drain"
        )

    async def stop(self) -> None:
        if self._skew_drain_task is not None:
            self._skew_drain_task.cancel()
            with contextlib.suppress(BaseException):
                await self._skew_drain_task
            self._skew_drain_task = None
        await self._ws.stop()
        await self._indexer.stop()
        await self._enricher_worker.stop()
        await self._writer.stop()
        await self._rest.aclose()
        await self._enricher.aclose()

    async def _drain_skew_periodically(self) -> None:
        while True:
            await asyncio.sleep(300)  # every 5 minutes
            skews = self._ws.drain_clock_skews_ms()
            if skews:
                await dao.record_clock_skews(self._db, skews)
