"""PolymarketTradesPoller unit tests.

We stub PolymarketREST so the tests exercise the poller's dedup + cursor +
queue semantics without any network.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from polysim.ingest.polymarket_trades_poller import PolymarketTradesPoller
from polysim.models import TradeEvent


class _FakeRest:
    """Test double for PolymarketREST — only needs `get_trades`."""

    def __init__(self, pages: list[list[TradeEvent]]) -> None:
        self._pages = list(pages)
        self.calls = 0

    async def get_trades(self, *, limit: int = 250) -> list[TradeEvent]:
        _ = limit
        self.calls += 1
        if not self._pages:
            return []
        return self._pages.pop(0)


def _t(id_: str, ts: datetime, wallet: str = "0xaf4") -> TradeEvent:
    return TradeEvent(
        id=id_,
        wallet_address=wallet,
        market_id="m1",
        side="BUY",
        outcome="YES",
        size_shares=100,
        price_cents=40,
        timestamp=ts,
    )


async def test_fresh_trades_flow_into_queue() -> None:
    now = datetime.now(UTC)
    rest = _FakeRest([[_t("t3", now), _t("t2", now - timedelta(seconds=1)), _t("t1", now - timedelta(seconds=2))]])
    poller = PolymarketTradesPoller(rest, poll_interval_s=0.05)  # type: ignore[arg-type]
    q: asyncio.Queue[TradeEvent] = asyncio.Queue()
    poller.subscribe(q)

    n = await poller._tick_once()
    assert n == 3
    assert q.qsize() == 3
    # Cursor advanced.
    assert poller._latest_ts == now.timestamp()


async def test_no_duplicates_on_repeat() -> None:
    now = datetime.now(UTC)
    page = [_t("t2", now), _t("t1", now - timedelta(seconds=1))]
    rest = _FakeRest([page, page, page])
    poller = PolymarketTradesPoller(rest)  # type: ignore[arg-type]
    q: asyncio.Queue[TradeEvent] = asyncio.Queue()
    poller.subscribe(q)

    n1 = await poller._tick_once()
    n2 = await poller._tick_once()
    n3 = await poller._tick_once()
    assert n1 == 2
    assert n2 == 0
    assert n3 == 0
    assert q.qsize() == 2


async def test_start_and_stop_clean() -> None:
    now = datetime.now(UTC)
    rest = _FakeRest([[_t("t1", now)]])
    poller = PolymarketTradesPoller(rest, poll_interval_s=0.02)  # type: ignore[arg-type]
    q: asyncio.Queue[TradeEvent] = asyncio.Queue()
    poller.subscribe(q)

    await poller.start()
    await asyncio.sleep(0.1)
    await poller.stop()
    assert q.qsize() >= 1


async def test_empty_page_is_fine() -> None:
    rest = _FakeRest([[], [], []])
    poller = PolymarketTradesPoller(rest)  # type: ignore[arg-type]
    assert await poller._tick_once() == 0
    assert poller.inserted_count == 0


async def test_seen_ids_are_bounded() -> None:
    now = datetime.now(UTC)
    # Feed 12k unique trades; seen_ids should trim to ≤10k.
    trades = [_t(f"id{i}", now - timedelta(seconds=i)) for i in range(12_000)]
    # Poller processes newest-first. Feed as one page.
    rest = _FakeRest([trades])
    poller = PolymarketTradesPoller(rest, page_size=12_000)  # type: ignore[arg-type]
    q: asyncio.Queue[TradeEvent] = asyncio.Queue()
    poller.subscribe(q)
    await poller._tick_once()
    assert len(poller._seen_ids) <= 10_000


@pytest.mark.integration
async def test_live_data_api_round_trip() -> None:
    """Hit the real data-api /trades endpoint. Skipped in quick runs."""
    from polysim.ingest.polymarket_rest import PolymarketREST

    async with PolymarketREST(
        gamma_base_url="https://gamma-api.polymarket.com",
        data_base_url="https://data-api.polymarket.com",
        clob_base_url="https://clob.polymarket.com",
    ) as rest:
        poller = PolymarketTradesPoller(rest, poll_interval_s=1.0)
        q: asyncio.Queue[TradeEvent] = asyncio.Queue()
        poller.subscribe(q)
        got = await poller._tick_once()
        # Polymarket is usually never this quiet, but don't fail if it is.
        assert got >= 0
