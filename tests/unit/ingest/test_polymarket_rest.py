"""Polymarket REST client tests — respx-mocked, no network."""

from __future__ import annotations

import httpx
import pytest
import respx

from polysim.ingest.polymarket_rest import PolymarketREST


@pytest.fixture
async def rest() -> PolymarketREST:
    client = PolymarketREST(rps_cap=2)
    try:
        yield client
    finally:
        await client.aclose()


@respx.mock
async def test_list_markets_happy_path(rest: PolymarketREST) -> None:
    respx.get("https://gamma-api.polymarket.com/markets").respond(
        200,
        json=[
            {
                "id": "m1",
                "slug": "claude5",
                "question": "Claude 5?",
                "createdAt": "2026-04-10T00:00:00Z",
                "endDate": "2026-09-30T00:00:00Z",
                "volume24hr": "47200",
            },
            {
                "id": "m2",
                "slug": "openai-o4",
                "question": "OpenAI o4?",
                "createdAt": "2026-04-01T00:00:00Z",
                "volume24hr": "850000",
            },
        ],
    )
    markets = await rest.list_markets(limit=2)
    assert len(markets) == 2
    assert markets[0].id == "m1"
    assert markets[0].daily_volume_usd_cents == 4_720_000
    assert markets[1].slug == "openai-o4"


@respx.mock
async def test_list_markets_envelope_shape(rest: PolymarketREST) -> None:
    respx.get("https://gamma-api.polymarket.com/markets").respond(
        200,
        json={
            "data": [
                {
                    "id": "m3",
                    "slug": "s",
                    "question": "Q?",
                    "createdAt": "2026-04-10T00:00:00Z",
                }
            ]
        },
    )
    markets = await rest.list_markets()
    assert len(markets) == 1 and markets[0].id == "m3"


@respx.mock
async def test_get_market_404(rest: PolymarketREST) -> None:
    respx.get("https://gamma-api.polymarket.com/markets/missing").respond(404)
    result = await rest.get_market("missing")
    assert result is None


@respx.mock
async def test_get_trades(rest: PolymarketREST) -> None:
    respx.get("https://data-api.polymarket.com/trades").respond(
        200,
        json=[
            {
                "id": "t1",
                "taker": "0xaf4",
                "market": "m1",
                "side": "BUY",
                "outcome": "YES",
                "size": "150",
                "price": "0.34",
                "timestamp": "2026-04-19T14:32:11Z",
                "transactionHash": "0xabc",
            }
        ],
    )
    trades = await rest.get_trades(market_id="m1")
    assert len(trades) == 1
    assert trades[0].wallet_address == "0xaf4"
    assert trades[0].price_cents == 34


@respx.mock
async def test_resolve_proxy_owner(rest: PolymarketREST) -> None:
    respx.get("https://data-api.polymarket.com/users").respond(
        200, json={"proxyWalletOwner": "0xOWNER123"}
    )
    owner = await rest.resolve_proxy_owner("0xproxy")
    assert owner == "0xowner123"


@respx.mock
async def test_resolve_proxy_owner_returns_none_on_error(rest: PolymarketREST) -> None:
    respx.get("https://data-api.polymarket.com/users").respond(404)
    assert await rest.resolve_proxy_owner("0xunknown") is None


@respx.mock
async def test_iter_trades_paginates(rest: PolymarketREST) -> None:
    # First page returns 2 trades (full batch), second returns empty → stop.
    respx.get("https://data-api.polymarket.com/trades", params={"limit": "2", "offset": "0", "market": "m1"}).respond(
        200,
        json=[
            {"id": "t1", "taker": "0x1", "market": "m1", "side": "BUY",
             "outcome": "YES", "size": 1, "price": 0.5, "timestamp": "2026-04-19T14:32:11Z"},
            {"id": "t2", "taker": "0x2", "market": "m1", "side": "SELL",
             "outcome": "NO", "size": 2, "price": 0.6, "timestamp": "2026-04-19T14:32:12Z"},
        ],
    )
    respx.get("https://data-api.polymarket.com/trades", params={"limit": "2", "offset": "2", "market": "m1"}).respond(
        200, json=[]
    )
    collected: list[str] = []
    async for t in rest.iter_trades(market_id="m1", page_size=2, max_pages=5):
        collected.append(t.id)
    assert collected == ["t1", "t2"]


@respx.mock
async def test_http_error_propagates_on_listing(rest: PolymarketREST) -> None:
    respx.get("https://gamma-api.polymarket.com/markets").respond(500)
    with pytest.raises(httpx.HTTPStatusError):
        await rest.list_markets()
