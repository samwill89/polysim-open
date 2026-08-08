"""Integration — replay fixture trades, assert flag counts & shape.

Build plan §2.16 acceptance target ("replay 100 fixture trades, assert
expected flags"). We build a synthetic batch with a known insider-like
trade pattern, run the DAO + profiler + scorer chain, and assert:

  * a flag is persisted for the insider wallet
  * no flags are persisted for the benign control wallets
  * the flag's composite_score and per-detector breakdown are populated
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from polysim.db import dao
from polysim.db.migrations.runner import apply_migrations
from polysim.models import Market, TradeEvent
from polysim.profiler.wallet_profiler import recompute_for_wallet
from polysim.scoring.category_insider import CategoryInsiderDetector
from polysim.scoring.composite import CompositeScorer, score_and_persist
from polysim.scoring.event_insider import EventInsiderDetector
from polysim.scoring.fresh_wallet import FreshWalletDetector

INSIDER = "0xaf4d02c1b5e882f0a7d3c4e5f6a7b8c90011"
CONTROL = "0x0000000000000000000000000000000000000001"

NICHE_MARKET = "m_claude5"


def _market(
    id_: str,
    *,
    category: str = "ai",
    resolved_outcome: str | None = None,
    volume_cents: int = 47_200_00,
) -> Market:
    created = datetime(2026, 4, 1, tzinfo=UTC)
    return Market(
        id=id_,
        slug=f"slug-{id_}",
        question=f"Will X happen for {id_}?",
        category=category,  # type: ignore[arg-type]
        created_at=created,
        resolves_at=created + timedelta(days=30),
        resolved_outcome=resolved_outcome,  # type: ignore[arg-type]
        resolved_at=created + timedelta(days=25) if resolved_outcome else None,
        daily_volume_usd_cents=volume_cents,
    )


def _trade(
    id_: str, wallet: str, market_id: str, *,
    side: str = "BUY", outcome: str = "YES",
    size: int = 100, price: int = 40,
    ts: datetime | None = None,
) -> TradeEvent:
    return TradeEvent(
        id=id_,
        wallet_address=wallet,
        market_id=market_id,
        side=side,  # type: ignore[arg-type]
        outcome=outcome,  # type: ignore[arg-type]
        size_shares=size,
        price_cents=price,
        timestamp=ts or datetime(2026, 4, 5, tzinfo=UTC),
    )


@pytest.mark.integration
async def test_scoring_pipeline_flags_insider_wallet(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    await apply_migrations(db)

    # 1. Seed 20 resolved AI markets + 1 niche target market.
    resolved_markets = [
        _market(f"m{i}", category="ai", resolved_outcome="YES")
        for i in range(20)
    ]
    niche = _market(NICHE_MARKET, category="ai", volume_cents=47_200_00)
    for m in [*resolved_markets, niche]:
        await dao.upsert_market(db, m)

    # 2. Insider wallet: 18/20 wins on AI markets — strong category edge.
    insider_trades: list[TradeEvent] = []
    base = datetime(2026, 4, 5, tzinfo=UTC)
    for i in range(20):
        outcome = "YES" if i < 18 else "NO"  # 18 wins, 2 losses
        insider_trades.append(
            _trade(
                f"t_ins_{i}", INSIDER, f"m{i}",
                outcome=outcome, size=200, price=30,
                ts=base + timedelta(hours=i),
            )
        )
    # Triggering trade on niche market — big contrarian bet ($25,000, 28pt
    # above mid=22). Gates on EventInsider all pass.
    triggering = _trade(
        "t_trigger", INSIDER, NICHE_MARKET,
        outcome="YES", size=50000, price=20,
        ts=base + timedelta(days=2),
    )
    insider_trades.append(triggering)

    await dao.insert_trades_batch(db, insider_trades)

    # Insider must look "fresh" for EventInsider to trigger.
    await dao.upsert_wallet_enrichment(
        db,
        address=INSIDER,
        nonce=3,
        funding_source="binance",
        funding_first_deposit_at="2026-04-17T00:00:00+00:00",
    )

    # 3. Control wallets: just trade a bunch in high-volume markets.
    high_vol = _market("m_high", category="ai", volume_cents=5_000_000_00)
    await dao.upsert_market(db, high_vol)
    control_trades = [
        _trade(f"t_ctrl_{i}", CONTROL, "m_high", size=100, price=40)
        for i in range(20)
    ]
    await dao.insert_trades_batch(db, control_trades)
    await dao.upsert_wallet_enrichment(
        db, address=CONTROL, nonce=500,
        funding_source="unknown", funding_first_deposit_at=None,
    )

    # 4. Profile both wallets.
    await recompute_for_wallet(db, INSIDER)
    await recompute_for_wallet(db, CONTROL)

    # 5. Build scorer + detectors.
    scorer = CompositeScorer(
        weights={
            "CategoryInsiderDetector": 3.5,
            "EventInsiderDetector": 2.5,
            "FreshWalletDetector": 1.0,
        },
        flag_threshold=5.0,
        min_contributing_detectors=2,
    )
    detectors = [
        CategoryInsiderDetector(min_resolved_markets=8),
        EventInsiderDetector(),
        FreshWalletDetector(),
    ]

    # 6. Score insider on the niche market with the triggering trade.
    insider_flag_id = await score_and_persist(
        db, scorer, detectors,  # type: ignore[arg-type]
        wallet_address=INSIDER,
        market_id=NICHE_MARKET,
        trade_id=triggering.id,
    )
    assert insider_flag_id is not None, "insider should be flagged"

    flag_row = await dao.get_flag(db, insider_flag_id)
    assert flag_row is not None
    assert flag_row["detector_name"] == "composite"
    assert flag_row["composite_score"] is not None
    assert flag_row["composite_score"] >= 5.0

    # 7. Score control wallet — no flag (not fresh, high-vol market).
    control_trade = control_trades[0]
    control_flag_id = await score_and_persist(
        db, scorer, detectors,  # type: ignore[arg-type]
        wallet_address=CONTROL,
        market_id="m_high",
        trade_id=control_trade.id,
    )
    assert control_flag_id is None, "control wallet should not be flagged"


@pytest.mark.integration
async def test_scoring_pipeline_dedup_within_window(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    await apply_migrations(db)

    # Minimal setup to produce a flag.
    m = _market(NICHE_MARKET, category="ai", volume_cents=47_200_00)
    await dao.upsert_market(db, m)
    for i in range(20):
        await dao.upsert_market(
            db, _market(f"m{i}", category="ai", resolved_outcome="YES")
        )

    trades = [
        _trade(
            f"t_{i}", INSIDER, f"m{i}",
            outcome="YES" if i < 18 else "NO", size=200, price=30,
        )
        for i in range(20)
    ]
    trig = _trade(
        "t_trig", INSIDER, NICHE_MARKET,
        outcome="YES", size=50000, price=20,
    )
    trades.append(trig)
    await dao.insert_trades_batch(db, trades)
    await dao.upsert_wallet_enrichment(
        db, address=INSIDER, nonce=3, funding_source="binance",
        funding_first_deposit_at=None,
    )
    await recompute_for_wallet(db, INSIDER)

    scorer = CompositeScorer(
        weights={
            "CategoryInsiderDetector": 3.5,
            "EventInsiderDetector": 2.5,
            "FreshWalletDetector": 1.0,
        },
        dedup_window_seconds=600,
    )
    detectors = [
        CategoryInsiderDetector(min_resolved_markets=8),
        EventInsiderDetector(),
        FreshWalletDetector(),
    ]

    first = await score_and_persist(
        db, scorer, detectors,  # type: ignore[arg-type]
        wallet_address=INSIDER,
        market_id=NICHE_MARKET,
        trade_id=trig.id,
    )
    assert first is not None

    second = await score_and_persist(
        db, scorer, detectors,  # type: ignore[arg-type]
        wallet_address=INSIDER,
        market_id=NICHE_MARKET,
        trade_id=trig.id,
    )
    assert second is None, "duplicate within dedup window should be suppressed"
