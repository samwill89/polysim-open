"""WS parser tests — pure function, no socket."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from polysim.ingest.polymarket_ws import parse_frame


def _trade_frame(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "event_type": "trade",
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
    base.update(overrides)
    return base


class TestParseFrame:
    def test_single_trade_object(self) -> None:
        frame = json.dumps(_trade_frame())
        trades, server_ts = parse_frame(frame)
        assert len(trades) == 1
        assert trades[0].id == "t1"
        assert trades[0].price_cents == 34
        assert server_ts == datetime(2026, 4, 19, 14, 32, 11, tzinfo=UTC)

    def test_array_of_trades(self) -> None:
        frame = json.dumps([_trade_frame(id="t1"), _trade_frame(id="t2")])
        trades, _ = parse_frame(frame)
        assert [t.id for t in trades] == ["t1", "t2"]

    def test_envelope_data_object(self) -> None:
        frame = json.dumps({"type": "trade", "data": _trade_frame()})
        trades, _ = parse_frame(frame)
        assert len(trades) == 1

    def test_envelope_events_array(self) -> None:
        frame = json.dumps(
            {"type": "trades", "events": [_trade_frame(id="t1"), _trade_frame(id="t2")]}
        )
        trades, _ = parse_frame(frame)
        assert [t.id for t in trades] == ["t1", "t2"]

    def test_non_trade_frame_returns_empty(self) -> None:
        frame = json.dumps({"type": "subscribed", "channel": "trades"})
        trades, _ = parse_frame(frame)
        assert trades == []

    def test_duck_typed_trade(self) -> None:
        """A dict with price+size+side counts as a trade even without event_type."""
        frame = json.dumps({
            "id": "t1", "taker": "0x1", "market": "m",
            "side": "BUY", "outcome": "YES", "size": 10, "price": 0.5,
            "timestamp": "2026-04-19T14:32:11Z",
        })
        trades, _ = parse_frame(frame)
        assert len(trades) == 1

    def test_garbage_returns_empty_no_raise(self) -> None:
        assert parse_frame("not json") == ([], None)
        assert parse_frame(b"\xff\xfe") == ([], None)  # bad utf-8
        assert parse_frame("null") == ([], None)

    def test_mixed_frame_keeps_valid(self) -> None:
        frame = json.dumps([_trade_frame(id="t1"), {"garbage": True}, _trade_frame(id="t2")])
        trades, _ = parse_frame(frame)
        assert [t.id for t in trades] == ["t1", "t2"]

    def test_bytes_frame(self) -> None:
        frame = json.dumps(_trade_frame()).encode("utf-8")
        trades, _ = parse_frame(frame)
        assert len(trades) == 1
