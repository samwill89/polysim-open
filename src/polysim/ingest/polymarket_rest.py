"""Polymarket REST client — READ-ONLY. Spec §4 / §14 #1.

Thin httpx wrapper over Polymarket's public APIs:
  - Gamma:  market metadata         (https://gamma-api.polymarket.com)
  - Data:   trades, user profiles   (https://data-api.polymarket.com)
  - CLOB:   orderbook snapshots     (https://clob.polymarket.com)

No order-placement methods exist on this class. The CI safety grep
(scripts/ci_safety.py) enforces the absence of such identifiers in our
source. We deliberately do NOT depend on py-clob-client because it
ships signing machinery we never use and want no part of (§14 #2).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Mapping
from typing import Any, cast

import httpx

from polysim.ingest.parsing import parse_gamma_market, parse_trade
from polysim.models import Market, TradeEvent

log = logging.getLogger(__name__)


DEFAULT_GAMMA_URL = "https://gamma-api.polymarket.com"
DEFAULT_DATA_URL = "https://data-api.polymarket.com"
DEFAULT_CLOB_URL = "https://clob.polymarket.com"


class PolymarketREST:
    """Async, rate-limited, read-only Polymarket client."""

    def __init__(
        self,
        *,
        gamma_base_url: str = DEFAULT_GAMMA_URL,
        data_base_url: str = DEFAULT_DATA_URL,
        clob_base_url: str = DEFAULT_CLOB_URL,
        timeout_s: float = 30.0,
        rps_cap: int = 10,
        user_agent: str = "polysim/0.1",
    ) -> None:
        self._gamma_url = gamma_base_url.rstrip("/")
        self._data_url = data_base_url.rstrip("/")
        self._clob_url = clob_base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            timeout=timeout_s, headers={"User-Agent": user_agent}
        )
        self._limiter = asyncio.Semaphore(max(1, rps_cap))

    async def __aenter__(self) -> PolymarketREST:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    # ── raw GET ───────────────────────────────────────────
    async def _get_json(
        self,
        url: str,
        params: Mapping[str, Any] | None = None,
    ) -> Any:
        async with self._limiter:
            resp = await self._client.get(url, params=dict(params) if params else None)
            resp.raise_for_status()
            return resp.json()

    # ── Markets ───────────────────────────────────────────
    async def list_markets(
        self,
        *,
        active: bool | None = None,
        closed: bool | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Market]:
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if active is not None:
            params["active"] = "true" if active else "false"
        if closed is not None:
            params["closed"] = "true" if closed else "false"
        raw = await self._get_json(f"{self._gamma_url}/markets", params=params)
        items = _unwrap_items(raw)
        parsed = [parse_gamma_market(x) for x in items]
        return [m for m in parsed if m is not None]

    async def get_market(self, market_id: str) -> Market | None:
        try:
            raw = await self._get_json(f"{self._gamma_url}/markets/{market_id}")
        except httpx.HTTPStatusError as e:
            log.warning("get_market(%s) failed: %s", market_id, e)
            return None
        return parse_gamma_market(raw)

    async def iter_markets(
        self,
        *,
        active: bool | None = None,
        page_size: int = 100,
        max_pages: int = 100,
    ) -> AsyncIterator[Market]:
        for page in range(max_pages):
            batch = await self.list_markets(
                active=active, limit=page_size, offset=page * page_size
            )
            if not batch:
                return
            for m in batch:
                yield m
            if len(batch) < page_size:
                return

    # ── Trades ────────────────────────────────────────────
    async def get_trades(
        self,
        *,
        market_id: str | None = None,
        user_address: str | None = None,
        limit: int = 500,
        offset: int = 0,
    ) -> list[TradeEvent]:
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if market_id is not None:
            params["market"] = market_id
        if user_address is not None:
            params["user"] = user_address
        try:
            raw = await self._get_json(f"{self._data_url}/trades", params=params)
        except httpx.HTTPStatusError as e:
            # Polymarket's data-api caps deep offset pagination with 400.
            # Treat that as "end of pagination" instead of crashing the caller.
            if e.response.status_code in (400, 404):
                log.debug(
                    "get_trades(market=%s, offset=%d) -> %d, treating as EOF",
                    market_id, offset, e.response.status_code,
                )
                return []
            raise
        items = _unwrap_items(raw)
        parsed = [parse_trade(x) for x in items]
        return [t for t in parsed if t is not None]

    async def iter_trades(
        self,
        *,
        market_id: str | None = None,
        page_size: int = 500,
        max_pages: int = 1000,
    ) -> AsyncIterator[TradeEvent]:
        for page in range(max_pages):
            batch = await self.get_trades(
                market_id=market_id, limit=page_size, offset=page * page_size
            )
            if not batch:
                return
            for t in batch:
                yield t
            if len(batch) < page_size:
                return

    # ── Orderbook ─────────────────────────────────────────
    async def get_orderbook(self, token_id: str) -> dict[str, Any]:
        raw = await self._get_json(
            f"{self._clob_url}/book", params={"token_id": token_id}
        )
        return cast(dict[str, Any], raw) if isinstance(raw, dict) else {}

    # ── Proxy resolution (G1) ─────────────────────────────
    async def resolve_proxy_owner(self, proxy_address: str) -> str | None:
        """Return the owner EOA for a Polymarket proxy wallet, or None.

        When Polymarket's Data API doesn't recognize the address or
        reports it as already an EOA, we return None (caller then stores
        owner_address=NULL, meaning "address IS the owner").
        """
        try:
            raw = await self._get_json(
                f"{self._data_url}/users",
                params={"address": proxy_address},
            )
        except httpx.HTTPStatusError:
            return None
        if not isinstance(raw, Mapping):
            return None
        owner = (
            raw.get("proxyWalletOwner")
            or raw.get("ownerAddress")
            or raw.get("owner")
        )
        return owner.lower() if isinstance(owner, str) else None


def _unwrap_items(raw: Any) -> list[Any]:
    if isinstance(raw, list):
        return raw
    if isinstance(raw, Mapping):
        data = raw.get("data")
        if isinstance(data, list):
            return data
    return []
