"""Robustness under injected faults — build plan §6.6.

Three scenarios:
  1. WS drops mid-session -> client reconnects without losing subscribers.
     (We test the pure frame parser against a reconnect-sequence fixture
      and the reconnect backoff loop against an injected error.)
  2. Alchemy returns 429 -> PolygonEnricher logs + returns a reasonable
     fallback rather than crashing.
  3. SQLite "database is locked" under concurrent write pressure ->
     DAO insert_trades_batch still makes forward progress.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import respx
from httpx import Response

from polysim.db import dao
from polysim.db.migrations.runner import apply_migrations
from polysim.ingest.funding_sources import FundingSources
from polysim.ingest.polygon_rpc import PolygonEnricher
from polysim.ingest.polymarket_ws import parse_frame
from polysim.models import TradeEvent

# ── 1. WS reconnect ──────────────────────────────────────


@pytest.mark.integration
def test_ws_parser_survives_malformed_frames_mid_session() -> None:
    """Real-world: WS reconnects deliver a mix of control frames, heartbeats,
    and malformed junk. parse_frame must never raise + must yield valid
    trades from valid frames."""
    good_frame = json.dumps(
        {
            "event_type": "trade",
            "id": "t42",
            "taker": "0xaf4",
            "market": "m1",
            "side": "BUY",
            "outcome": "YES",
            "size": 100,
            "price": 0.4,
            "timestamp": datetime.now(UTC).isoformat(),
        }
    )
    heartbeats_and_garbage = [
        "",
        "null",
        "{}",
        "not-json",
        json.dumps({"type": "heartbeat"}),
        json.dumps({"type": "ping"}),
        json.dumps({"type": "subscribed", "channel": "trades"}),
        b"\xff\xfe\xfd",  # bad utf-8 bytes
    ]
    # Good frame interspersed with garbage — must parse exactly 1 trade.
    all_frames = [*heartbeats_and_garbage, good_frame, *heartbeats_and_garbage]
    total_trades = 0
    for f in all_frames:
        trades, _ = parse_frame(f)
        total_trades += len(trades)
    assert total_trades == 1


@pytest.mark.integration
async def test_ws_ingestor_reconnects_after_error() -> None:
    """PolymarketWSIngestor's _run loop catches WebSocketException and
    enters backoff. We verify the backoff respects the stop event by
    driving one iteration and stopping."""
    from polysim.ingest.polymarket_ws import PolymarketWSIngestor

    ing = PolymarketWSIngestor(
        ws_url="wss://invalid.example/does-not-exist",
        max_backoff_s=0.1,
    )
    await ing.start()
    # Give it ~300ms to hit the connect error + start backoff.
    await asyncio.sleep(0.3)
    await ing.stop()
    # If stop() returns cleanly the backoff/reconnect loop is well-behaved.


# ── 2. Alchemy 429 ───────────────────────────────────────


_ALCHEMY_URL = "https://polygon-mainnet.g.alchemy.com/v2/test_key"


@respx.mock
@pytest.mark.integration
async def test_polygon_enricher_handles_429_without_crash() -> None:
    """Alchemy rate-limits -> enricher logs + raises; no silent corruption."""
    respx.post(_ALCHEMY_URL).mock(
        return_value=Response(429, json={"error": {"message": "rate limited"}})
    )
    e = PolygonEnricher(
        api_key="test_key",
        funding_sources=FundingSources({}),
        rps_cap=2,
    )
    try:
        # The client retries 429s with backoff, then surfaces a RuntimeError
        # (not a raw httpx error) so callers see one stable failure type.
        with pytest.raises(RuntimeError, match="429"):
            await e.get_nonce("0xaf4")
    finally:
        await e.aclose()


@respx.mock
@pytest.mark.integration
async def test_polygon_get_first_inbound_tolerates_429() -> None:
    """get_first_inbound returns (None, None) on 429 rather than propagating."""
    respx.post(_ALCHEMY_URL).mock(
        return_value=Response(429, json={"error": {"message": "rate limited"}})
    )
    e = PolygonEnricher(
        api_key="test_key",
        funding_sources=FundingSources({}),
    )
    try:
        from_addr, ts = await e.get_first_inbound("0xaf4")
        assert from_addr is None
        assert ts is None
    finally:
        await e.aclose()


# ── 3. SQLite "database is locked" ───────────────────────


@pytest.mark.integration
async def test_insert_trades_batch_succeeds_under_concurrency(
    tmp_path: Path,
) -> None:
    """Concurrent writes -> SQLite serializes + ours commits. No
    'database is locked' bubbles up to the caller."""
    db = tmp_path / "t.db"
    await apply_migrations(db)

    base = datetime(2026, 4, 1, tzinfo=UTC)

    async def _writer(idx_start: int, n: int) -> int:
        batch = [
            TradeEvent(
                id=f"t_{idx_start + i}",
                wallet_address=f"0xw{idx_start + i}",
                market_id=f"m_{(idx_start + i) % 5}",
                side="BUY",
                outcome="YES",
                size_shares=10,
                price_cents=40,
                timestamp=base,
            )
            for i in range(n)
        ]
        return await dao.insert_trades_batch(db, batch)

    # 4 writers each push 25 trades concurrently -> 100 trades total.
    results = await asyncio.gather(
        _writer(0, 25),
        _writer(25, 25),
        _writer(50, 25),
        _writer(75, 25),
    )
    assert sum(results) == 100

    # Idempotency still holds under concurrency — same ids -> no new rows.
    second_pass = await asyncio.gather(
        _writer(0, 25),
        _writer(25, 25),
    )
    assert sum(second_pass) == 0


@pytest.mark.integration
async def test_dao_survives_migration_mismatch(tmp_path: Path) -> None:
    """If a caller uses a DB file that exists but lacks our schema
    (e.g. operator pointed at a wrong file), reads must return empty
    rather than throwing OperationalError into the caller."""
    # Create an empty DB without migrations.
    db = tmp_path / "bare.db"
    import aiosqlite
    async with aiosqlite.connect(str(db)) as conn:
        await conn.execute("CREATE TABLE unrelated (x INTEGER)")
        await conn.commit()

    # All read-paths should degrade gracefully.
    assert await dao.db_stats(db) == {"trades": 0, "wallets": 0, "markets": 0, "flags": 0}
    assert await dao.list_active_runs(db) == []
    assert await dao.list_paper_runs(db) == []
    assert await dao.get_flag(db, 1) is None
    assert await dao.get_position(db, 1) is None
    assert await dao.list_open_positions(db, 1) == []
