"""Investigator agent tests — injection-safe JSON parsing, cap, routing."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from polysim.investigator.agent import Investigator, _parse_verdict_json
from polysim.models import (
    Flag,
    InvestigatorOutcome,
    Market,
    TradeEvent,
    WalletProfile,
)


def _flag(composite: float = 8.4) -> Flag:
    return Flag(
        wallet_address="0xaf4",
        market_id="m1",
        trade_id="t1",
        detector_name="composite",
        raw_score=composite,
        composite_score=composite,
        components={},
        created_at=datetime(2026, 4, 19, tzinfo=UTC),
    )


def _profile() -> WalletProfile:
    return WalletProfile(
        wallet_address="0xaf4",
        as_of=datetime(2026, 4, 19, tzinfo=UTC),
        total_markets=10,
        resolved_markets=8,
        wins=7,
        losses=1,
        win_rate=0.875,
        total_pnl_cents=100_000,
        categories={"ai": 8},
        category_exclusivity=1.0,
        avg_entry_to_resolution_hours=48.0,
    )


def _market() -> Market:
    return Market(
        id="m1",
        slug="claude5",
        question="Will Claude 5 launch?",
        category="ai",
        created_at=datetime(2026, 4, 10, tzinfo=UTC),
        daily_volume_usd_cents=47_200_00,
    )


def _trade() -> TradeEvent:
    return TradeEvent(
        id="t1",
        wallet_address="0xaf4",
        market_id="m1",
        side="BUY",
        outcome="YES",
        size_shares=150,
        price_cents=32,
        timestamp=datetime(2026, 4, 19, 13, 44, tzinfo=UTC),
    )


class TestParseVerdictJson:
    def test_clean_json(self) -> None:
        text = (
            '{"verdict": "INFORMED", "confidence": 0.82, '
            '"reasoning": "Fresh wallet + contrarian", '
            '"red_flags": ["fresh"], "green_flags": []}'
        )
        out = _parse_verdict_json(text)
        assert out is not None
        assert out.verdict == "INFORMED"
        assert out.confidence == 0.82
        assert out.red_flags == ["fresh"]

    def test_fenced_json(self) -> None:
        text = '```json\n{"verdict":"LUCKY","confidence":0.4,"reasoning":"ok"}\n```'
        out = _parse_verdict_json(text)
        assert out is not None and out.verdict == "LUCKY"

    def test_confidence_clamped(self) -> None:
        text = '{"verdict":"INFORMED","confidence":5.0,"reasoning":""}'
        out = _parse_verdict_json(text)
        assert out is not None and out.confidence == 1.0

    def test_unknown_verdict_rejected(self) -> None:
        text = '{"verdict":"MAYBE","confidence":0.5,"reasoning":""}'
        assert _parse_verdict_json(text) is None

    def test_malformed_returns_none(self) -> None:
        assert _parse_verdict_json("not json") is None
        assert _parse_verdict_json("") is None

    def test_non_object_returns_none(self) -> None:
        assert _parse_verdict_json("[]") is None
        assert _parse_verdict_json('"string"') is None


class TestInvestigator:
    def _make(self, *, client: Any | None = None, cap: int = 100) -> Investigator:
        return Investigator(api_key="test", max_calls_per_day=cap, client=client)

    async def test_api_error_returns_default_unclear(self) -> None:
        client = MagicMock()
        client.messages = MagicMock()
        client.messages.create = AsyncMock(side_effect=RuntimeError("boom"))
        inv = self._make(client=client)
        out, usage = await inv.investigate(_flag(), _profile(), [_trade()], _market())
        assert isinstance(out, InvestigatorOutcome)
        assert out.verdict == "UNCLEAR"
        # On API error we don't have usage to log.
        assert usage is None

    async def test_daily_cap_returns_none_after_cap(self) -> None:
        # Build a fake response the first time; 2nd call should not be made.
        response = MagicMock()
        block = MagicMock()
        block.text = '{"verdict":"INFORMED","confidence":0.9,"reasoning":"ok"}'
        response.content = [block]
        response.usage = MagicMock()
        response.usage.input_tokens = 100
        response.usage.output_tokens = 50
        response.usage.cache_creation_input_tokens = 0
        response.usage.cache_read_input_tokens = 0

        client = MagicMock()
        client.messages = MagicMock()
        client.messages.create = AsyncMock(return_value=response)

        inv = self._make(client=client, cap=1)
        out1, usage1 = await inv.investigate(_flag(), _profile(), [_trade()], _market())
        assert out1 is not None and out1.verdict == "INFORMED"
        assert usage1 is not None and usage1.input_tokens == 100

        out2, usage2 = await inv.investigate(_flag(), _profile(), [_trade()], _market())
        assert out2 is None and usage2 is None  # cap hit → fallback signal
        # Only one actual API call happened.
        assert client.messages.create.await_count == 1

    async def test_should_act_rule(self) -> None:
        inv = self._make()
        assert inv.should_act(InvestigatorOutcome(verdict="INFORMED", confidence=0.9, reasoning=""))
        assert not inv.should_act(InvestigatorOutcome(verdict="INFORMED", confidence=0.5, reasoning=""))
        assert not inv.should_act(InvestigatorOutcome(verdict="LUCKY", confidence=1.0, reasoning=""))
        assert not inv.should_act(InvestigatorOutcome(verdict="UNCLEAR", confidence=1.0, reasoning=""))

    def _response(self, text: str) -> MagicMock:
        response = MagicMock()
        block = MagicMock()
        block.text = text
        response.content = [block]
        response.usage = MagicMock()
        response.usage.input_tokens = 100
        response.usage.output_tokens = 50
        response.usage.cache_creation_input_tokens = 0
        response.usage.cache_read_input_tokens = 0
        return response

    async def test_routes_to_opus_for_high_composite(self) -> None:
        response = self._response('{"verdict":"INFORMED","confidence":0.9,"reasoning":""}')
        client = MagicMock()
        client.messages = MagicMock()
        client.messages.create = AsyncMock(return_value=response)

        inv = self._make(client=client)
        await inv.investigate(_flag(composite=8.5), _profile(), [_trade()], _market())
        call = client.messages.create.call_args
        assert call.kwargs["model"] == inv.model

    async def test_routes_to_haiku_for_low_composite(self) -> None:
        response = self._response('{"verdict":"UNCLEAR","confidence":0.3,"reasoning":""}')
        client = MagicMock()
        client.messages = MagicMock()
        client.messages.create = AsyncMock(return_value=response)

        inv = self._make(client=client)
        await inv.investigate(_flag(composite=5.5), _profile(), [_trade()], _market())
        call = client.messages.create.call_args
        assert call.kwargs["model"] == inv.triage_model

    async def test_sends_cache_control_on_system_and_history(self) -> None:
        """Phase 3 §3.2 — the system prompt and wallet-history block must
        carry cache_control so repeat calls on the same wallet hit cache."""
        response = self._response('{"verdict":"UNCLEAR","confidence":0.3,"reasoning":""}')
        client = MagicMock()
        client.messages = MagicMock()
        client.messages.create = AsyncMock(return_value=response)

        inv = self._make(client=client)
        await inv.investigate(_flag(), _profile(), [_trade()], _market())
        kwargs = client.messages.create.call_args.kwargs

        # system is a list of text blocks, first has cache_control.
        system = kwargs["system"]
        assert isinstance(system, list) and len(system) >= 1
        assert system[0]["cache_control"] == {"type": "ephemeral"}

        # First user content block is the cached wallet-history one.
        user_content = kwargs["messages"][0]["content"]
        assert isinstance(user_content, list) and len(user_content) >= 2
        assert user_content[0].get("cache_control") == {"type": "ephemeral"}
        # Second block (trigger context) must NOT be cached.
        assert "cache_control" not in user_content[1]

    async def test_usage_flows_into_stats_object(self) -> None:
        response = self._response('{"verdict":"INFORMED","confidence":0.9,"reasoning":""}')
        response.usage.input_tokens = 3200
        response.usage.output_tokens = 380
        response.usage.cache_creation_input_tokens = 1500
        response.usage.cache_read_input_tokens = 0
        client = MagicMock()
        client.messages = MagicMock()
        client.messages.create = AsyncMock(return_value=response)

        inv = self._make(client=client)
        _, usage = await inv.investigate(_flag(composite=8.5), _profile(), [_trade()], _market())
        assert usage is not None
        assert usage.input_tokens == 3200
        assert usage.output_tokens == 380
        assert usage.cache_creation_tokens == 1500
        assert usage.cache_read_tokens == 0
        assert usage.cost_cents > 0
