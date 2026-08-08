"""Pure parser tests — no network, no asyncio."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from polysim.ingest.parsing import (
    parse_gamma_market,
    parse_outcome,
    parse_price_cents,
    parse_resolution_outcome,
    parse_side,
    parse_size_shares,
    parse_timestamp,
    parse_trade,
)


class TestTimestamp:
    def test_unix_seconds(self) -> None:
        dt = parse_timestamp(1_745_000_000)
        assert dt is not None and dt.tzinfo == UTC

    def test_unix_milliseconds(self) -> None:
        dt = parse_timestamp(1_745_000_000_000)
        assert dt is not None
        # 10^12 threshold: should be interpreted as ms
        assert dt.year >= 2020

    def test_iso_with_z(self) -> None:
        dt = parse_timestamp("2026-04-19T14:32:11Z")
        assert dt == datetime(2026, 4, 19, 14, 32, 11, tzinfo=UTC)

    def test_iso_with_offset(self) -> None:
        dt = parse_timestamp("2026-04-19T14:32:11+00:00")
        assert dt == datetime(2026, 4, 19, 14, 32, 11, tzinfo=UTC)

    def test_naive_iso_assumed_utc(self) -> None:
        dt = parse_timestamp("2026-04-19T14:32:11")
        assert dt is not None and dt.tzinfo == UTC

    def test_garbage(self) -> None:
        assert parse_timestamp("not-a-date") is None
        assert parse_timestamp(None) is None
        assert parse_timestamp([]) is None


class TestPrice:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (0.32, 32),
            ("0.32", 32),
            (0, 0),
            (1, 100),          # edge: 0..1 form
            (0.999, 100),      # rounds to nearest
            (32, 32),          # integer percent form
            (100, 100),        # max percent
            ("0.055", 6),      # rounds up
        ],
    )
    def test_accepts_range(self, raw: object, expected: int) -> None:
        assert parse_price_cents(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        [-0.1, 1.5, 50.5, 200, "abc", None, True, False],
    )
    def test_rejects(self, raw: object) -> None:
        assert parse_price_cents(raw) is None


class TestSize:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("100", 100), (100.0, 100), (100.7, 101), (0, 0)],
    )
    def test_accepts(self, raw: object, expected: int) -> None:
        assert parse_size_shares(raw) == expected

    def test_rejects_negative(self) -> None:
        assert parse_size_shares(-1) is None


class TestSide:
    def test_uppercase(self) -> None:
        assert parse_side("BUY") == "BUY"
        assert parse_side("SELL") == "SELL"

    def test_mixed_case(self) -> None:
        assert parse_side("buy") == "BUY"

    def test_int_encoding(self) -> None:
        assert parse_side(0) == "BUY"
        assert parse_side(1) == "SELL"

    def test_garbage(self) -> None:
        assert parse_side("hold") is None


class TestOutcome:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("YES", "YES"), ("no", "NO"), ("true", "YES"), ("N", "NO"),
         (0, "YES"), (1, "NO"), (True, "YES"), (False, "NO")],
    )
    def test_accepts(self, raw: object, expected: str) -> None:
        assert parse_outcome(raw) == expected

    def test_garbage(self) -> None:
        assert parse_outcome("maybe") is None

    def test_resolution_outcome_accepts_invalid(self) -> None:
        assert parse_resolution_outcome("INVALID") == "INVALID"
        assert parse_resolution_outcome("YES") == "YES"


class TestGammaMarket:
    def test_minimal_valid(self) -> None:
        raw = {
            "id": "m1",
            "slug": "will-openai-x",
            "question": "Will OpenAI X?",
            "createdAt": "2026-04-10T00:00:00Z",
        }
        m = parse_gamma_market(raw)
        assert m is not None
        assert m.id == "m1"
        assert m.slug == "will-openai-x"

    def test_prefers_condition_id(self) -> None:
        raw = {
            "conditionId": "0xabc",
            "id": "fallback",
            "slug": "s",
            "question": "Q?",
            "createdAt": "2026-04-10T00:00:00Z",
        }
        m = parse_gamma_market(raw)
        assert m is not None and m.id == "0xabc"

    def test_volume_to_cents(self) -> None:
        raw = {
            "id": "m1", "slug": "s", "question": "Q?",
            "createdAt": "2026-04-10T00:00:00Z",
            "volume24hr": "12.34",
        }
        m = parse_gamma_market(raw)
        assert m is not None and m.daily_volume_usd_cents == 1234

    def test_missing_required_returns_none(self) -> None:
        assert parse_gamma_market({"id": "m1"}) is None
        assert parse_gamma_market(None) is None
        assert parse_gamma_market("not a dict") is None


class TestTrade:
    def test_polymarket_data_shape(self) -> None:
        raw = {
            "transactionHash": "0xabc",
            "eventIndex": 0,
            "taker": "0xaf4d02",
            "market": "m_claude5",
            "side": "BUY",
            "outcome": "YES",
            "size": "150",
            "price": "0.34",
            "timestamp": "2026-04-19T14:32:11Z",
        }
        t = parse_trade(raw)
        assert t is not None
        assert t.wallet_address == "0xaf4d02"
        assert t.market_id == "m_claude5"
        assert t.price_cents == 34
        assert t.size_shares == 150
        assert t.tx_hash == "0xabc"

    def test_uses_id_when_present(self) -> None:
        raw = {
            "id": "trade-123",
            "taker": "0x1",
            "market": "m",
            "side": "BUY",
            "outcome": "YES",
            "size": 10,
            "price": 0.5,
            "timestamp": "2026-04-19T14:32:11Z",
        }
        t = parse_trade(raw)
        assert t is not None and t.id == "trade-123"

    def test_missing_field_returns_none(self) -> None:
        raw = {
            "id": "t1",
            "taker": "0x1",
            "market": "m",
            "side": "BUY",
            # outcome missing
            "size": 10,
            "price": 0.5,
            "timestamp": "2026-04-19T14:32:11Z",
        }
        assert parse_trade(raw) is None

    def test_tx_hash_optional(self) -> None:
        raw = {
            "id": "t1",
            "taker": "0x1",
            "market": "m",
            "side": "BUY",
            "outcome": "YES",
            "size": 10,
            "price": 0.5,
            "timestamp": "2026-04-19T14:32:11Z",
        }
        t = parse_trade(raw)
        assert t is not None and t.tx_hash is None
