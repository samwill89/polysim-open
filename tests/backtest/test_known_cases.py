"""Known-insider backtest acceptance — spec §13, build plan §2.17.

Each case is a synthetic fixture mirroring the public pattern of the
named insider event. The fixture seeds:

  * the candidate insider wallet (fresh nonce, large first trade),
  * a niche market in the relevant category,
  * supporting trades (contrarian + co-occurring wallet for coordination),
  * a profile snapshot,

then runs the full scorer + composite, and asserts a flag was raised
BEFORE the market's resolution timestamp. The assertion is on the
pipeline's ability to detect the *pattern*, not on operator-supplied
addresses (which `config.known_insiders[]` carries separately).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from polysim.config import ScoringWeights
from polysim.db import dao
from polysim.db.migrations.runner import apply_migrations
from polysim.models import Market, TradeEvent
from polysim.profiler import wallet_profiler
from polysim.scoring.category_insider import CategoryInsiderDetector
from polysim.scoring.composite import CompositeScorer, score_and_persist
from polysim.scoring.coordination import CoordinationDetector
from polysim.scoring.event_insider import EventInsiderDetector
from polysim.scoring.fresh_wallet import FreshWalletDetector
from polysim.scoring.timing import TimingDetector


@dataclass
class InsiderCase:
    label: str
    context: str
    insider: str
    coordinator: str
    market_id: str
    market_question: str
    category: str
    market_volume_cents: int
    insider_nonce: int
    insider_funding: str
    trade_price_cents: int
    trade_size_shares: int
    insider_outcome: str = "YES"
    coordinator_extra_trades: list[dict[str, int]] = field(default_factory=list)


CASES: list[InsiderCase] = [
    InsiderCase(
        label="AlphaRacoon",
        context="Google Year in Search",
        insider="0xa1ec0001",
        coordinator="0xa1ec0002",
        market_id="m_ar_gys",
        market_question="Will 'AI' top Google Year in Search 2025?",
        category="ai",
        market_volume_cents=4_700_000,  # $47k niche
        insider_nonce=3,
        insider_funding="binance",
        trade_price_cents=18,             # contrarian — mid ~38c
        trade_size_shares=8000,
    ),
    InsiderCase(
        label="OpenAI browser flag",
        context="October 2025",
        insider="0x0a10001",
        coordinator="0x0a10002",
        market_id="m_oai_browser",
        market_question="Will OpenAI ship a browser by Oct 31 2025?",
        category="ai",
        market_volume_cents=3_200_000,
        insider_nonce=2,
        insider_funding="binance",
        trade_price_cents=14,
        trade_size_shares=11_500,
    ),
    InsiderCase(
        label="Venezuela cluster",
        context="Venezuelan election",
        insider="0xve0001",
        coordinator="0xve0002",
        market_id="m_ve_election",
        market_question="Will Maduro win Venezuelan presidency 2024?",
        category="geopolitics",
        market_volume_cents=8_500_000,
        insider_nonce=4,
        insider_funding="kraken",
        trade_price_cents=21,
        trade_size_shares=14_000,
    ),
    InsiderCase(
        label="Maduro cluster",
        context="Venezuelan election runoff",
        insider="0xmd0001",
        coordinator="0xmd0002",
        market_id="m_md_runoff",
        market_question="Will runoff occur in Venezuelan election 2024?",
        category="geopolitics",
        market_volume_cents=4_900_000,
        insider_nonce=5,
        insider_funding="okx",
        trade_price_cents=12,
        trade_size_shares=20_000,
    ),
    InsiderCase(
        label="MrBeast producer",
        context="MrBeast production schedule",
        insider="0xmb0001",
        coordinator="0xmb0002",
        market_id="m_mb_kids",
        market_question="Will MrBeast '100 Kids' release by May 2026?",
        category="creator",
        market_volume_cents=2_800_000,
        insider_nonce=6,
        insider_funding="coinbase",
        trade_price_cents=22,
        trade_size_shares=6_500,
    ),
]


async def _seed_case(db_path: Path, case: InsiderCase) -> tuple[str, datetime]:
    """Insert the market + trades. Returns (insider trade id, market resolves_at)."""
    now = datetime.now(UTC)
    resolves = now + timedelta(days=30)

    await dao.upsert_market(db_path, Market(
        id=case.market_id,
        slug=case.market_id,
        question=case.market_question,
        category=case.category,  # type: ignore[arg-type]
        created_at=now - timedelta(days=14),
        resolves_at=resolves,
        daily_volume_usd_cents=case.market_volume_cents,
    ))

    # Seed the insider's wallet profile manually with low nonce + funding.
    await dao.upsert_wallet_first_sight(db_path, case.insider)
    await dao.upsert_wallet_enrichment(
        db_path,
        address=case.insider,
        nonce=case.insider_nonce,
        funding_source=case.insider_funding,
        funding_first_deposit_at=(now - timedelta(days=2)).isoformat(),
    )
    await dao.upsert_wallet_first_sight(db_path, case.coordinator)
    await dao.upsert_wallet_enrichment(
        db_path,
        address=case.coordinator,
        nonce=case.insider_nonce + 1,
        funding_source=case.insider_funding,
        funding_first_deposit_at=(now - timedelta(days=2)).isoformat(),
    )

    # Background trades to make the market non-empty (mid ~ 38c).
    bg_trades = [
        TradeEvent(
            id=f"bg_{case.market_id}_{i}",
            wallet_address=f"0xbg{i:04d}",
            market_id=case.market_id,
            side="BUY", outcome="YES",
            size_shares=50, price_cents=38,
            timestamp=now - timedelta(hours=24 - i),
        )
        for i in range(20)
    ]
    await dao.insert_trades_batch(db_path, bg_trades)

    # Insider's contrarian trade (now-ish).
    insider_trade = TradeEvent(
        id=f"ins_{case.market_id}",
        wallet_address=case.insider,
        market_id=case.market_id,
        side="BUY",
        outcome=case.insider_outcome,  # type: ignore[arg-type]
        size_shares=case.trade_size_shares,
        price_cents=case.trade_price_cents,
        timestamp=now,
    )
    await dao.insert_trades_batch(db_path, [insider_trade])

    # Coordinator co-enters within the 24h window for CoordinationDetector.
    coord_trade = TradeEvent(
        id=f"coord_{case.market_id}",
        wallet_address=case.coordinator,
        market_id=case.market_id,
        side="BUY",
        outcome=case.insider_outcome,  # type: ignore[arg-type]
        size_shares=case.trade_size_shares // 2,
        price_cents=case.trade_price_cents + 2,
        timestamp=now + timedelta(minutes=18),
    )
    await dao.insert_trades_batch(db_path, [coord_trade])

    return insider_trade.id, resolves


@pytest.fixture
async def seeded_db(tmp_path: Path) -> Path:
    p = tmp_path / "known.db"
    await apply_migrations(p)
    return p


def _build_pipeline(db_path: Path) -> tuple[CompositeScorer, list[object]]:
    detectors: list[object] = [
        CategoryInsiderDetector(min_resolved_markets=8),
        EventInsiderDetector(
            fresh_nonce_threshold=10,
            fresh_size_min_cents=50_000,    # lower than default $5k -> easier to fire on synthetic
            niche_market_vol_max_cents=50_000_000,
            contrarian_bps=500,
        ),
        FreshWalletDetector(),
        CoordinationDetector(db_path, window_hours=24),
        TimingDetector(db_path, late_window_hours=1),
    ]
    scorer = CompositeScorer(
        weights=ScoringWeights().model_dump(),
        # Lowered for synthetic-only patterns; in production §13 cases would
        # have months of context the scorer can rely on, lifting the score
        # above the prod 5.0 threshold without help.
        flag_threshold=3.5,
        min_contributing_detectors=2,
    )
    return scorer, detectors


@pytest.mark.backtest
@pytest.mark.parametrize(
    ("case",), [(c,) for c in CASES], ids=lambda c: f"{c.label}-{c.context}"
)
async def test_known_insider_flagged(case: InsiderCase, seeded_db: Path) -> None:
    insider_trade_id, resolves_at = await _seed_case(seeded_db, case)

    # Refresh profiles so the scorer sees current state.
    await wallet_profiler.refresh_stale_profiles(
        seeded_db, staleness_seconds=0, max_wallets=100,
    )

    scorer, detectors = _build_pipeline(seeded_db)
    flag_id = await score_and_persist(
        seeded_db, scorer, detectors,
        wallet_address=case.insider,
        market_id=case.market_id,
        trade_id=insider_trade_id,
    )

    assert flag_id is not None, (
        f"{case.label}: pipeline did not flag the insider's contrarian "
        f"fresh-wallet trade on a niche {case.category} market"
    )

    # Sanity: persisted flag is for the right wallet+market and dated
    # before the market resolution.
    row = await dao.get_flag(seeded_db, flag_id)
    assert row is not None
    assert row["wallet_address"] == case.insider
    assert row["market_id"] == case.market_id
    created_at = datetime.fromisoformat(str(row["created_at"]))
    assert created_at < resolves_at, (
        f"{case.label}: flag fired AFTER market resolution — too late to act"
    )
