"""Polygon JSON-RPC enricher tests — respx-mocked."""

from __future__ import annotations

import pytest
import respx
from httpx import Response

from polysim.ingest.funding_sources import FundingSources
from polysim.ingest.polygon_rpc import PolygonEnricher, safe_rpc_error

_URL = "https://polygon-mainnet.g.alchemy.com/v2/test_key"


@pytest.fixture
async def enricher() -> PolygonEnricher:
    funding = FundingSources({"0xbinance0000000000000000000000000000000000": "binance"})
    e = PolygonEnricher(api_key="test_key", funding_sources=funding, rps_cap=2)
    try:
        yield e
    finally:
        await e.aclose()


def _rpc_response(result: object) -> Response:
    return Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})


@respx.mock
async def test_get_nonce(enricher: PolygonEnricher) -> None:
    respx.post(_URL).mock(return_value=_rpc_response("0x3"))
    nonce = await enricher.get_nonce("0xaf4")
    assert nonce == 3


@respx.mock
async def test_enrich_full(enricher: PolygonEnricher) -> None:
    # Route all POSTs to the same URL but return different responses in order.
    route = respx.post(_URL)
    route.side_effect = [
        _rpc_response("0x5"),  # nonce
        _rpc_response(
            {
                "transfers": [
                    {
                        "from": "0xBINANCE0000000000000000000000000000000000",
                        "metadata": {"blockTimestamp": "2026-04-10T12:00:00Z"},
                    }
                ]
            }
        ),
    ]
    result = await enricher.enrich("0xaf4")
    assert result.address == "0xaf4"
    assert result.nonce == 5
    assert result.funding_source == "binance"
    assert result.funding_first_deposit_at is not None


@respx.mock
async def test_enrich_unknown_funding_source(enricher: PolygonEnricher) -> None:
    route = respx.post(_URL)
    route.side_effect = [
        _rpc_response("0x7"),
        _rpc_response(
            {
                "transfers": [
                    {
                        "from": "0xnotanexchange000000000000000000000000",
                        "metadata": {"blockTimestamp": "2026-04-10T12:00:00Z"},
                    }
                ]
            }
        ),
    ]
    result = await enricher.enrich("0xaf5")
    assert result.nonce == 7
    assert result.funding_source == "unknown"


@respx.mock
async def test_enrich_no_transfers(enricher: PolygonEnricher) -> None:
    route = respx.post(_URL)
    route.side_effect = [
        _rpc_response("0x0"),
        _rpc_response({"transfers": []}),
    ]
    result = await enricher.enrich("0xaf6")
    assert result.nonce == 0
    assert result.funding_source is None
    assert result.funding_first_deposit_at is None


@respx.mock
async def test_rpc_error_raises(enricher: PolygonEnricher) -> None:
    respx.post(_URL).mock(
        return_value=Response(200, json={"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "boom"}})
    )
    with pytest.raises(RuntimeError, match="RPC error"):
        await enricher.get_nonce("0xaf4")


@respx.mock
async def test_429_retries_then_succeeds() -> None:
    e = PolygonEnricher(api_key="test_key", max_retries=1, retry_base_s=0)
    try:
        route = respx.post(_URL)
        route.side_effect = [
            Response(429, headers={"retry-after": "0"}),
            _rpc_response("0x4"),
        ]

        nonce = await e.get_nonce("0xaf4")

        assert nonce == 4
        assert route.call_count == 2
    finally:
        await e.aclose()


@respx.mock
async def test_repeated_rpc_failures_open_circuit() -> None:
    e = PolygonEnricher(
        api_key="test_key",
        max_retries=0,
        retry_base_s=0,
        circuit_failures=1,
        circuit_cooldown_s=30,
    )
    try:
        route = respx.post(_URL).mock(return_value=Response(429))

        with pytest.raises(RuntimeError, match="RPC HTTP 429"):
            await e.get_nonce("0xaf4")

        with pytest.raises(RuntimeError, match="RPC circuit open"):
            await e.get_nonce("0xaf4")

        assert route.call_count == 1
    finally:
        await e.aclose()


@respx.mock
async def test_http_errors_do_not_include_alchemy_key() -> None:
    e = PolygonEnricher(api_key="test_key", max_retries=0)
    try:
        respx.post(_URL).mock(return_value=Response(401))

        with pytest.raises(RuntimeError) as exc_info:
            await e.get_nonce("0xaf4")

        assert "401" in str(exc_info.value)
        assert "test_key" not in str(exc_info.value)
        assert _URL not in str(exc_info.value)
    finally:
        await e.aclose()


def test_safe_rpc_error_redacts_alchemy_url_tokens() -> None:
    err = RuntimeError(f"failed for url {_URL}")

    redacted = safe_rpc_error(err)

    assert "test_key" not in redacted
    assert "https://polygon-mainnet.g.alchemy.com/v2/<redacted>" in redacted
