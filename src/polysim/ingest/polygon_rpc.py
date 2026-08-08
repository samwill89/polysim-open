"""Polygon RPC enrichment — nonce + first inbound transfer.

Build plan §1.3 + §1.8.  Uses Alchemy's JSON-RPC directly (httpx) rather
than web3.py to keep the venv free of signing surface (§14 #2).  Only
read methods — `eth_getTransactionCount` and `alchemy_getAssetTransfers`.

Rate-limited via asyncio.Semaphore to stay inside Alchemy's free-tier
limits (default 10 req/s).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from polysim.ingest.funding_sources import FundingSources

log = logging.getLogger(__name__)

_ALCHEMY_URL_RE = re.compile(r"(https://[^\s\"')]+/v2/)[^\s\"')/]+")


def safe_rpc_error(exc: BaseException) -> str:
    """Return an exception string with Alchemy URL tokens redacted."""
    text = str(exc)
    text = _ALCHEMY_URL_RE.sub(r"\1<redacted>", text)
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def _retry_after_seconds(resp: httpx.Response) -> float | None:
    raw = resp.headers.get("retry-after")
    if not raw:
        return None
    try:
        return max(0.0, min(60.0, float(raw)))
    except ValueError:
        pass
    try:
        retry_at = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=UTC)
    return max(0.0, min(60.0, (retry_at - datetime.now(UTC)).total_seconds()))


@dataclass(frozen=True)
class WalletEnrichment:
    address: str
    nonce: int
    funding_source: str | None           # 'binance', 'coinbase', 'unknown', or None
    funding_first_deposit_at: datetime | None


class PolygonEnricher:
    def __init__(
        self,
        api_key: str,
        *,
        funding_sources: FundingSources | None = None,
        base_url_template: str = "https://polygon-mainnet.g.alchemy.com/v2/{api_key}",
        rps_cap: int = 10,
        timeout_s: float = 30.0,
        max_retries: int = 3,
        retry_base_s: float = 1.0,
        circuit_failures: int = 5,
        circuit_cooldown_s: float = 60.0,
    ) -> None:
        if not api_key:
            log.warning("PolygonEnricher started with empty API key — calls will fail")
        self._url = base_url_template.format(api_key=api_key or "missing")
        self._client = httpx.AsyncClient(timeout=timeout_s)
        self._limiter = asyncio.Semaphore(max(1, rps_cap))
        self._funding = funding_sources or FundingSources({})
        self._max_retries = max(0, max_retries)
        self._retry_base_s = max(0.0, retry_base_s)
        self._circuit_failures = max(1, circuit_failures)
        self._circuit_cooldown_s = max(0.0, circuit_cooldown_s)
        self._consecutive_failures = 0
        self._circuit_open_until = 0.0

    async def __aenter__(self) -> PolygonEnricher:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    def _assert_circuit_closed(self, method: str) -> None:
        remaining = self._circuit_open_until - time.monotonic()
        if remaining > 0:
            raise RuntimeError(
                f"RPC circuit open for {method}; retry after {remaining:.1f}s"
            )

    def _record_rpc_success(self) -> None:
        self._consecutive_failures = 0
        self._circuit_open_until = 0.0

    def _record_rpc_failure(self, method: str, reason: str) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures < self._circuit_failures:
            return
        self._circuit_open_until = time.monotonic() + self._circuit_cooldown_s
        log.warning(
            "Polygon RPC circuit opened after %s consecutive failure(s); method=%s reason=%s",
            self._consecutive_failures,
            method,
            reason,
        )

    async def _rpc(self, method: str, params: list[Any]) -> Any:
        self._assert_circuit_closed(method)
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        for attempt in range(self._max_retries + 1):
            async with self._limiter:
                try:
                    resp = await self._client.post(self._url, json=payload)
                except httpx.HTTPError as exc:
                    if attempt >= self._max_retries:
                        self._record_rpc_failure(method, type(exc).__name__)
                        raise RuntimeError(
                            f"RPC transport error for {method}: {type(exc).__name__}"
                        ) from exc
                    await asyncio.sleep(
                        min(30.0, self._retry_base_s * (2 ** attempt))
                    )
                    continue

            if resp.status_code == 429:
                if attempt >= self._max_retries:
                    self._record_rpc_failure(method, "http-429")
                    raise RuntimeError(f"RPC HTTP 429 for {method} after retries")
                retry_after = _retry_after_seconds(resp)
                await asyncio.sleep(
                    retry_after
                    if retry_after is not None
                    else min(30.0, self._retry_base_s * (2 ** attempt))
                )
                continue
            if resp.status_code >= 500:
                if attempt >= self._max_retries:
                    self._record_rpc_failure(method, f"http-{resp.status_code}")
                    raise RuntimeError(f"RPC HTTP {resp.status_code} for {method}")
                await asyncio.sleep(min(30.0, self._retry_base_s * (2 ** attempt)))
                continue
            if resp.status_code >= 400:
                self._record_rpc_failure(method, f"http-{resp.status_code}")
                raise RuntimeError(f"RPC HTTP {resp.status_code} for {method}")
            data = resp.json()
            break
        else:  # pragma: no cover - for-loop always returns or raises.
            raise RuntimeError(f"RPC retry loop exhausted for {method}")
        if isinstance(data, dict) and "error" in data:
            self._record_rpc_failure(method, "rpc-error")
            raise RuntimeError(f"RPC error for {method}: {data['error']}")
        self._record_rpc_success()
        return data.get("result") if isinstance(data, dict) else None

    async def get_nonce(self, address: str) -> int:
        result = await self._rpc("eth_getTransactionCount", [address.lower(), "latest"])
        if isinstance(result, str) and result.startswith("0x"):
            return int(result, 16)
        raise RuntimeError(f"unexpected nonce result for {address}: {result!r}")

    async def get_first_inbound(
        self, address: str
    ) -> tuple[str | None, datetime | None]:
        """Return (from_address_lower, utc_timestamp) of the earliest
        inbound transfer, or (None, None) if none or lookup failed.

        Relies on Alchemy's enhanced `alchemy_getAssetTransfers`. Polygon
        POS chain. `maxCount` is a hex-encoded string per Alchemy's API.
        """
        params = [
            {
                "toAddress": address.lower(),
                "category": ["external", "erc20"],
                "order": "asc",
                "maxCount": "0x1",
                "fromBlock": "0x0",
                "toBlock": "latest",
                "withMetadata": True,
            }
        ]
        try:
            result = await self._rpc("alchemy_getAssetTransfers", params)
        except (httpx.HTTPError, RuntimeError) as exc:
            log.warning(
                "getAssetTransfers failed for %s: %s",
                address,
                safe_rpc_error(exc),
            )
            return None, None
        if not isinstance(result, dict):
            return None, None
        transfers = result.get("transfers")
        if not isinstance(transfers, list) or not transfers:
            return None, None
        first = transfers[0]
        if not isinstance(first, dict):
            return None, None

        from_addr_raw = first.get("from")
        from_addr = (
            from_addr_raw.lower()
            if isinstance(from_addr_raw, str) and from_addr_raw
            else None
        )

        ts: datetime | None = None
        metadata = first.get("metadata")
        if isinstance(metadata, dict):
            block_ts = metadata.get("blockTimestamp")
            if isinstance(block_ts, str):
                s = block_ts.rstrip("Z")
                try:
                    dt = datetime.fromisoformat(s)
                    ts = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
                except ValueError:
                    ts = None
        return from_addr, ts

    async def enrich(self, address: str) -> WalletEnrichment:
        address_l = address.lower()
        nonce = await self.get_nonce(address_l)
        from_addr, ts = await self.get_first_inbound(address_l)
        classified = self._funding.classify(from_addr)
        source: str | None
        if classified is not None:
            source = classified
        elif from_addr is not None:
            source = "unknown"
        else:
            source = None
        return WalletEnrichment(
            address=address_l,
            nonce=nonce,
            funding_source=source,
            funding_first_deposit_at=ts,
        )
