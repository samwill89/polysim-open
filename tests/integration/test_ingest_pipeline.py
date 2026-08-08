"""Integration — feed 100 fixture WS frames through the pipeline, assert persistence.

Build plan §1.11 acceptance target: "replay 100 fixture WS frames, assert
100 trades landed". This test exercises parse_frame + TradeWriter + DAO
without touching the network or the live WS connection.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from polysim.db import dao
from polysim.db.migrations.runner import apply_migrations
from polysim.ingest.pipeline import TradeWriter
from polysim.ingest.polymarket_ws import parse_frame
from polysim.models import TradeEvent


def _frame(i: int) -> str:
    return json.dumps(
        {
            "event_type": "trade",
            "id": f"t{i:04d}",
            "taker": f"0xw{i % 20:02d}",
            "market": f"0xm{i % 5:02d}",
            "side": "BUY" if i % 2 == 0 else "SELL",
            "outcome": "YES" if i % 3 != 0 else "NO",
            "size": str(100 + i),
            "price": f"0.{30 + i % 60:02d}",
            "timestamp": "2026-04-19T14:32:11Z",
            "transactionHash": f"0x{i:064x}",
        }
    )


@pytest.mark.integration
async def test_100_frame_replay_persists_all_trades(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    await apply_migrations(db)

    # 1) Parse 100 frames → TradeEvents.
    trades: list[TradeEvent] = []
    for i in range(100):
        parsed, _ = parse_frame(_frame(i))
        trades.extend(parsed)
    assert len(trades) == 100

    # 2) Feed into a TradeWriter and drain.
    q: asyncio.Queue[TradeEvent] = asyncio.Queue()
    writer = TradeWriter(db, q, batch_size=25, max_latency_s=0.1)
    wallet_q: asyncio.Queue[str] = asyncio.Queue()
    writer.set_wallet_sight_queue(wallet_q)

    for t in trades:
        await q.put(t)

    await writer.start()

    # Wait until the writer's insert counter reaches 100, or timeout.
    for _ in range(100):
        if writer.inserted_count >= 100:
            break
        await asyncio.sleep(0.05)

    await writer.stop()

    assert writer.inserted_count == 100
    stats = await dao.db_stats(db)
    assert stats["trades"] == 100
    # 20 unique wallets, 5 unique markets referenced in the test generator.
    assert stats["wallets"] == 20
    assert stats["markets"] == 5

    # Wallet-sight queue should have the 20 unique wallet addresses.
    seen: set[str] = set()
    while not wallet_q.empty():
        seen.add(wallet_q.get_nowait())
    assert len(seen) == 20


@pytest.mark.integration
async def test_replay_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    await apply_migrations(db)

    # First pass: 50 trades.
    trades = [parse_frame(_frame(i))[0][0] for i in range(50)]
    assert await dao.insert_trades_batch(db, trades) == 50

    # Second pass: same 50 trades (same ids).
    assert await dao.insert_trades_batch(db, trades) == 0

    stats = await dao.db_stats(db)
    assert stats["trades"] == 50
