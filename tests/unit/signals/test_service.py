"""Service-layer tests: snapshot → score → persist → serve, on a temp DB
with the StaticProvider (fully deterministic, no network)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from polysim.config import Config
from polysim.db import dao
from polysim.db.migrations.runner import apply_migrations
from polysim.models import Market
from polysim.signals.providers import StaticProvider
from polysim.signals.schema import ConversationPost, TopicActivity
from polysim.signals.service import (
    format_signal_note,
    latest_market_signal,
    recent_market_signals,
    run_snapshot_once,
    topic_history,
    write_topic_snapshot,
)

NOW = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
async def db(tmp_path: Path) -> Path:
    path = tmp_path / "t.db"
    await apply_migrations(path)
    return path


def _cfg(**signals: Any) -> Config:
    return Config.model_validate({
        "run": {"name": "t", "mode": "live_paper"},
        "categories": {},
        "intel_sources": {"box_office": {"reddit": ["boxoffice"]}},
        "signals": signals,
    })


def _posts(n: int, *, title: str, hours_spread: float = 12.0) -> list[ConversationPost]:
    return [
        ConversationPost(
            source="reddit", topic="boxoffice", external_id=f"p{i}",
            created_at=NOW - timedelta(hours=(i % max(1, int(hours_spread))) + 0.5),
            title=f"{title} #{i}", text="discussion", author=f"user{i}",
            score=10, num_comments=6,
        )
        for i in range(n)
    ]


async def _seed_market(db: Path) -> Market:
    m = Market(
        id="mkt-dune", slug="dune-part-three-100m",
        question='Will "Dune: Part Three" gross $100M on opening weekend?',
        category="box_office",
        created_at=datetime(2026, 6, 1, tzinfo=UTC),
        daily_volume_usd_cents=1_000_000,
    )
    await dao.upsert_market(db, m)
    return m


async def _seed_history(db: Path, *, n_rows: int, n_posts: int) -> None:
    """Prior topic snapshots so z-scores/velocity have a baseline.

    Counts wobble ±1 around `n_posts` — a zero-variance history would
    make every z-score 0 by construction.
    """
    for i in range(n_rows):
        end = NOW - timedelta(days=i + 1)
        posts = n_posts + (i % 3) - 1
        await write_topic_snapshot(db, TopicActivity(
            source="static", topic="boxoffice",
            window_start=end - timedelta(hours=24), window_end=end,
            n_posts=posts, n_comments=posts * 3,
            score_sum=posts * 10, unique_authors=posts,
        ))


async def test_snapshot_writes_topic_and_market_rows(db: Path) -> None:
    market = await _seed_market(db)
    await _seed_history(db, n_rows=6, n_posts=10)
    provider = StaticProvider({
        "boxoffice": _posts(30, title="Dune: Part Three tracking"),
    })
    r = await run_snapshot_once(
        db, _cfg(), provider, now=NOW, markets=[market],
    )
    assert r["topics_fetched"] == 1
    assert r["signals_written"] == 1

    hist = await topic_history(
        db, source="static", topic="boxoffice", before=NOW + timedelta(hours=1),
    )
    assert len(hist) == 7  # 6 seeded + 1 fresh

    sig = await latest_market_signal(db, market.id, now=NOW)
    assert sig is not None
    assert sig.market_id == market.id
    assert sig.n_matched_posts > 0
    # 30 posts vs baseline of 10/day → strong spike.
    assert sig.attention_z > 2.0
    assert sig.composite > 0.6
    assert sig.confidence > 0.5
    assert 0.0 <= sig.composite <= 1.0


async def test_spike_scores_higher_than_calm(db: Path) -> None:
    market = await _seed_market(db)
    await _seed_history(db, n_rows=6, n_posts=10)
    calm = StaticProvider({"boxoffice": _posts(10, title="Dune: Part Three")})
    spike = StaticProvider({"boxoffice": _posts(40, title="Dune: Part Three")})

    await run_snapshot_once(db, _cfg(), calm, now=NOW, markets=[market])
    calm_sig = await latest_market_signal(db, market.id, now=NOW)
    # Second snapshot an hour later (history now includes the calm row).
    await run_snapshot_once(
        db, _cfg(), spike, now=NOW + timedelta(hours=1), markets=[market],
    )
    spike_sig = await latest_market_signal(
        db, market.id, now=NOW + timedelta(hours=1),
    )
    assert calm_sig is not None and spike_sig is not None
    assert spike_sig.composite > calm_sig.composite


async def test_failed_provider_writes_nothing(db: Path) -> None:
    market = await _seed_market(db)
    provider = StaticProvider({})  # every topic → None (fetch failed)
    r = await run_snapshot_once(db, _cfg(), provider, now=NOW, markets=[market])
    assert r["topics_failed"] == 1
    assert r["signals_written"] == 0
    assert await latest_market_signal(db, market.id, now=NOW) is None


async def test_latest_signal_staleness_window(db: Path) -> None:
    market = await _seed_market(db)
    await _seed_history(db, n_rows=6, n_posts=10)
    provider = StaticProvider({"boxoffice": _posts(20, title="Dune: Part Three")})
    await run_snapshot_once(db, _cfg(), provider, now=NOW, markets=[market])

    fresh = await latest_market_signal(
        db, market.id, max_age_hours=24, now=NOW + timedelta(hours=2),
    )
    stale = await latest_market_signal(
        db, market.id, max_age_hours=24, now=NOW + timedelta(hours=48),
    )
    assert fresh is not None
    assert stale is None


async def test_no_term_match_falls_back_to_community_tide(db: Path) -> None:
    market = await _seed_market(db)
    await _seed_history(db, n_rows=6, n_posts=10)
    provider = StaticProvider({
        "boxoffice": _posts(20, title="Unrelated superhero chatter"),
    })
    await run_snapshot_once(db, _cfg(), provider, now=NOW, markets=[market])
    sig = await latest_market_signal(db, market.id, now=NOW)
    assert sig is not None
    assert sig.n_matched_posts == 0
    assert sig.confidence <= 0.5  # community-fallback cap
    assert sig.components.get("community_fallback") is True


async def test_recent_signals_join_question(db: Path) -> None:
    market = await _seed_market(db)
    await _seed_history(db, n_rows=6, n_posts=10)
    provider = StaticProvider({"boxoffice": _posts(20, title="Dune: Part Three")})
    await run_snapshot_once(db, _cfg(), provider, now=NOW, markets=[market])
    rows = await recent_market_signals(db, limit=5)
    assert rows
    assert "Dune" in str(rows[0]["question"])


async def test_format_signal_note_is_one_line(db: Path) -> None:
    market = await _seed_market(db)
    await _seed_history(db, n_rows=6, n_posts=10)
    provider = StaticProvider({"boxoffice": _posts(20, title="Dune: Part Three")})
    await run_snapshot_once(db, _cfg(), provider, now=NOW, markets=[market])
    sig = await latest_market_signal(db, market.id, now=NOW)
    assert sig is not None
    note = format_signal_note(sig)
    assert "\n" not in note
    assert "r/boxoffice" in note
