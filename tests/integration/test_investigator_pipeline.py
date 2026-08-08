"""Integration — investigate_flag roundtrip with a mocked Anthropic client.

Build plan §3.7 — verifies:
  - flag_costs row written with correct cost from mocked usage
  - flags.investigator_verdict + acted_on updated
  - second call against same wallet+trade_count hits cache_read path
    (we assert via usage reporting — mock returns cache_read_tokens on call 2)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from polysim.db import dao
from polysim.db.migrations.runner import apply_migrations
from polysim.investigator.agent import Investigator, investigate_flag
from polysim.models import Flag, Market, TradeEvent, WalletProfile


def _response(
    text: str,
    *,
    input_tokens: int = 200,
    output_tokens: int = 60,
    cache_creation: int = 0,
    cache_read: int = 0,
) -> MagicMock:
    r = MagicMock()
    block = MagicMock()
    block.text = text
    r.content = [block]
    r.usage = MagicMock()
    r.usage.input_tokens = input_tokens
    r.usage.output_tokens = output_tokens
    r.usage.cache_creation_input_tokens = cache_creation
    r.usage.cache_read_input_tokens = cache_read
    return r


async def _seed(db: Path) -> int:
    """Seed DB with minimal entities + one flag. Returns flag id."""
    # Market
    await dao.upsert_market(
        db,
        Market(
            id="m1",
            slug="s",
            question="Will Claude 5 launch by Q3?",
            category="ai",
            created_at=datetime(2026, 4, 10, tzinfo=UTC),
            resolves_at=datetime(2026, 9, 30, tzinfo=UTC),
            daily_volume_usd_cents=47_200_00,
        ),
    )
    # Wallet + profile + trade
    await dao.upsert_wallet_first_sight(db, "0xaf4")
    await dao.upsert_wallet_enrichment(
        db, address="0xaf4", nonce=3,
        funding_source="binance", funding_first_deposit_at=None,
    )
    await dao.insert_trades_batch(
        db,
        [
            TradeEvent(
                id="t1",
                wallet_address="0xaf4",
                market_id="m1",
                side="BUY",
                outcome="YES",
                size_shares=5000,
                price_cents=32,
                timestamp=datetime(2026, 4, 19, 13, 44, tzinfo=UTC),
            )
        ],
    )
    await dao.write_wallet_profile(
        db,
        WalletProfile(
            wallet_address="0xaf4",
            as_of=datetime.now(UTC),
            total_markets=10,
            resolved_markets=8,
            wins=7,
            losses=1,
            win_rate=0.875,
            total_pnl_cents=100_000,
            categories={"ai": 8},
            category_exclusivity=1.0,
            avg_entry_to_resolution_hours=48.0,
        ),
    )
    # Flag
    flag_id = await dao.write_flag(
        db,
        Flag(
            wallet_address="0xaf4",
            market_id="m1",
            trade_id="t1",
            detector_name="composite",
            raw_score=4.5,
            composite_score=8.4,
            components={"contrib": {"EventInsiderDetector": 2.35}},
            created_at=datetime.now(UTC),
        ),
    )
    assert flag_id is not None
    return flag_id


@pytest.mark.integration
async def test_investigate_flag_persists_verdict_and_cost(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    await apply_migrations(db)
    flag_id = await _seed(db)

    client = MagicMock()
    client.messages = MagicMock()
    client.messages.create = AsyncMock(
        return_value=_response(
            '{"verdict":"INFORMED","confidence":0.82,'
            '"reasoning":"fresh + contrarian + niche",'
            '"red_flags":["fresh wallet"],"green_flags":[]}',
            input_tokens=3000,
            output_tokens=400,
            cache_creation=1800,
        )
    )
    inv = Investigator(api_key="t", client=client, max_calls_per_day=10)

    outcome = await investigate_flag(db, inv, flag_id)
    assert outcome is not None
    assert outcome.verdict == "INFORMED"
    assert outcome.confidence == 0.82

    # Flag row updated.
    row = await dao.get_flag(db, flag_id)
    assert row is not None
    assert row["investigator_verdict"] == "INFORMED"
    assert row["acted_on"] == 1  # confidence 0.82 >= 0.6 threshold
    assert "fresh wallet" in str(row["investigator_reasoning"])

    # Cost row present + non-zero.
    cost = await dao.get_flag_cost(db, flag_id)
    assert cost is not None
    assert cost["model"] == "claude-opus-4-7"  # composite 8.4 -> Opus
    assert int(cost["input_tokens"]) == 3000
    assert int(cost["cached_tokens"]) == 1800
    assert int(cost["cost_cents"]) > 0


@pytest.mark.integration
async def test_investigate_flag_uncategorised_verdicts_not_acted_on(
    tmp_path: Path,
) -> None:
    db = tmp_path / "t.db"
    await apply_migrations(db)
    flag_id = await _seed(db)

    client = MagicMock()
    client.messages = MagicMock()
    client.messages.create = AsyncMock(
        return_value=_response(
            '{"verdict":"LUCKY","confidence":0.9,"reasoning":"prior experience"}'
        )
    )
    inv = Investigator(api_key="t", client=client)
    outcome = await investigate_flag(db, inv, flag_id)
    assert outcome is not None and outcome.verdict == "LUCKY"
    row = await dao.get_flag(db, flag_id)
    assert row is not None
    assert row["investigator_verdict"] == "LUCKY"
    assert row["acted_on"] == 0


@pytest.mark.integration
async def test_investigate_flag_missing_flag_returns_none(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    await apply_migrations(db)
    client = MagicMock()
    client.messages = MagicMock()
    client.messages.create = AsyncMock()
    inv = Investigator(api_key="t", client=client)
    assert await investigate_flag(db, inv, 9999) is None
    client.messages.create.assert_not_awaited()


@pytest.mark.integration
async def test_investigate_flag_cache_hit_on_repeat_call(tmp_path: Path) -> None:
    """Phase 3 §3.7 acceptance — repeat call against same wallet/trade_count
    carries cache_control and (in this mocked flow) produces cache_read tokens."""
    db = tmp_path / "t.db"
    await apply_migrations(db)
    flag_id_1 = await _seed(db)

    # Second flag on the SAME wallet + market; wallet trade-count unchanged.
    flag_id_2 = await dao.write_flag(
        db,
        Flag(
            wallet_address="0xaf4",
            market_id="m1",
            trade_id="t1",
            detector_name="composite",
            raw_score=4.5,
            composite_score=8.4,
            components={},
            created_at=datetime.now(UTC) + timedelta(seconds=30),
        ),
    )
    assert flag_id_2 is not None

    client = MagicMock()
    client.messages = MagicMock()
    # First call: fresh cache write.
    # Second call: cache hit — same wallet block (history unchanged).
    client.messages.create = AsyncMock(
        side_effect=[
            _response(
                '{"verdict":"UNCLEAR","confidence":0.3,"reasoning":"ok"}',
                cache_creation=1800,
                cache_read=0,
            ),
            _response(
                '{"verdict":"UNCLEAR","confidence":0.3,"reasoning":"ok"}',
                cache_creation=0,
                cache_read=1800,
            ),
        ]
    )
    inv = Investigator(api_key="t", client=client)

    await investigate_flag(db, inv, flag_id_1)
    await investigate_flag(db, inv, flag_id_2)

    # Both calls had cache_control on system + first user block.
    for call in client.messages.create.await_args_list:
        kwargs = call.kwargs
        assert kwargs["system"][0]["cache_control"] == {"type": "ephemeral"}
        user_blocks = kwargs["messages"][0]["content"]
        assert user_blocks[0]["cache_control"] == {"type": "ephemeral"}
        assert "cache_control" not in user_blocks[1]

    # Cost row on 2nd flag should reflect cache-read savings.
    cost2 = await dao.get_flag_cost(db, flag_id_2)
    assert cost2 is not None
    assert int(cost2["cached_tokens"]) == 1800


@pytest.mark.integration
async def test_investigate_flag_cap_hit_returns_none(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    await apply_migrations(db)
    flag_id = await _seed(db)

    client = MagicMock()
    client.messages = MagicMock()
    client.messages.create = AsyncMock(
        return_value=_response('{"verdict":"UNCLEAR","confidence":0.1,"reasoning":""}')
    )
    inv = Investigator(api_key="t", client=client, max_calls_per_day=0)
    outcome = await investigate_flag(db, inv, flag_id)
    assert outcome is None
    cost = await dao.get_flag_cost(db, flag_id)
    assert cost is None  # no call -> no cost row

    # API not touched.
    client.messages.create.assert_not_awaited()


@pytest.mark.integration
async def test_score_and_persist_invokes_investigator_on_high_composite(
    tmp_path: Path,
) -> None:
    """Verifies the wire-up added in Phase 3."""
    from polysim.scoring.category_insider import CategoryInsiderDetector
    from polysim.scoring.composite import CompositeScorer, score_and_persist
    from polysim.scoring.event_insider import EventInsiderDetector
    from polysim.scoring.fresh_wallet import FreshWalletDetector

    db = tmp_path / "t.db"
    await apply_migrations(db)

    # Seed 20 resolved AI markets + niche target + insider wallet history.
    base = datetime(2026, 4, 5, tzinfo=UTC)
    for i in range(20):
        await dao.upsert_market(
            db,
            Market(
                id=f"m{i}",
                slug=f"s{i}",
                question=f"Q{i}?",
                category="ai",
                created_at=base,
                resolves_at=base + timedelta(days=30),
                resolved_outcome="YES",
                resolved_at=base + timedelta(days=25),
                daily_volume_usd_cents=100_000_00,
            ),
        )
    await dao.upsert_market(
        db,
        Market(
            id="m_target",
            slug="target",
            question="Will Claude 5 launch by Q3?",
            category="ai",
            created_at=base,
            resolves_at=base + timedelta(days=30),
            daily_volume_usd_cents=47_200_00,
        ),
    )
    trades: list[TradeEvent] = []
    for i in range(20):
        trades.append(
            TradeEvent(
                id=f"t_{i}",
                wallet_address="0xaf4",
                market_id=f"m{i}",
                side="BUY",
                outcome="YES" if i < 18 else "NO",
                size_shares=200,
                price_cents=30,
                timestamp=base + timedelta(hours=i),
            )
        )
    trig = TradeEvent(
        id="t_trig",
        wallet_address="0xaf4",
        market_id="m_target",
        side="BUY",
        outcome="YES",
        size_shares=50_000,
        price_cents=20,
        timestamp=base + timedelta(days=2),
    )
    trades.append(trig)
    await dao.insert_trades_batch(db, trades)
    await dao.upsert_wallet_enrichment(
        db, address="0xaf4", nonce=3, funding_source="binance",
        funding_first_deposit_at=None,
    )
    from polysim.profiler.wallet_profiler import recompute_for_wallet
    await recompute_for_wallet(db, "0xaf4")

    scorer = CompositeScorer(
        weights={
            "CategoryInsiderDetector": 3.5,
            "EventInsiderDetector": 2.5,
            "FreshWalletDetector": 1.0,
        },
        flag_threshold=5.0,
        min_contributing_detectors=2,
    )
    detectors: list[Any] = [
        CategoryInsiderDetector(min_resolved_markets=8),
        EventInsiderDetector(),
        FreshWalletDetector(),
    ]

    client = MagicMock()
    client.messages = MagicMock()
    client.messages.create = AsyncMock(
        return_value=_response(
            '{"verdict":"INFORMED","confidence":0.85,"reasoning":"pattern match",'
            '"red_flags":["fresh + contrarian"],"green_flags":[]}'
        )
    )
    inv = Investigator(api_key="t", client=client, max_calls_per_day=10)

    flag_id = await score_and_persist(
        db, scorer, detectors,
        wallet_address="0xaf4",
        market_id="m_target",
        trade_id=trig.id,
        investigator=inv,
        min_composite_to_invoke=5.0,
    )
    assert flag_id is not None
    # Investigator was called once.
    assert client.messages.create.await_count == 1

    # Verdict + cost row persisted.
    row = await dao.get_flag(db, flag_id)
    assert row is not None
    assert row["investigator_verdict"] == "INFORMED"
    assert row["acted_on"] == 1
    cost = await dao.get_flag_cost(db, flag_id)
    assert cost is not None
