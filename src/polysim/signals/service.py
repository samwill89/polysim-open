"""Signal-layer orchestration: fetch → extract → score → persist → serve.

DB access lives here (direct aiosqlite, same pattern as the equity lane).
Everything network-y is provider-mediated and failure-tolerant: a topic
that can't be fetched simply produces no snapshot this tick, and a market
without a fresh signal row reads as `None` — which every consumer maps
to neutral (multiplier 1.0, gate open).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite

from polysim.config import Config
from polysim.models import Market
from polysim.signals.extract import (
    activity_from_posts,
    attention_zscore,
    breadth_norm,
    engagement_norm,
    match_posts,
    stance_of_posts,
    velocity_ratio,
)
from polysim.signals.linker import MarketTopicLink, category_topics_from_config, link_market
from polysim.signals.providers import ConversationProvider
from polysim.signals.schema import ConversationPost, MarketSignal, TopicActivity
from polysim.signals.scoring import compose_conviction, signal_confidence
from polysim.utils.time import iso, now_utc, parse_iso

log = logging.getLogger(__name__)


# ── persistence ──────────────────────────────────────────


async def write_topic_snapshot(db_path: Path, activity: TopicActivity) -> int:
    async with aiosqlite.connect(str(db_path)) as db:
        cur = await db.execute(
            """
            INSERT INTO conversation_snapshots(
                ts, source, topic, window_hours, n_posts, n_comments,
                score_sum, unique_authors, posts_per_hour
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                iso(activity.window_end), activity.source, activity.topic,
                activity.window_hours, activity.n_posts, activity.n_comments,
                activity.score_sum, activity.unique_authors,
                activity.posts_per_hour,
            ),
        )
        new_id = int(cur.lastrowid or 0)
        await cur.close()
        await db.commit()
    return new_id


async def topic_history(
    db_path: Path,
    *,
    source: str,
    topic: str,
    before: datetime,
    limit: int = 30,
) -> list[tuple[int, float]]:
    """Prior (n_posts, posts_per_hour) rows for a topic, newest first."""
    if not db_path.exists():
        return []
    try:
        async with aiosqlite.connect(str(db_path)) as db, db.execute(
            "SELECT n_posts, posts_per_hour FROM conversation_snapshots "
            "WHERE source = ? AND topic = ? AND ts < ? "
            "ORDER BY ts DESC LIMIT ?",
            (source, topic, iso(before), limit),
        ) as cur:
            rows = await cur.fetchall()
    except aiosqlite.OperationalError:
        return []
    return [(int(r[0]), float(r[1])) for r in rows]


async def write_market_signal(db_path: Path, sig: MarketSignal) -> int:
    async with aiosqlite.connect(str(db_path)) as db:
        cur = await db.execute(
            """
            INSERT INTO market_signals(
                ts, market_id, category, provider, topics_json,
                matched_terms_json, n_posts, n_matched_posts, attention_z,
                velocity, engagement, breadth, stance, composite,
                confidence, components_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                iso(sig.ts), sig.market_id, sig.category, sig.provider,
                json.dumps(list(sig.topics)),
                json.dumps(list(sig.matched_terms)),
                sig.n_posts, sig.n_matched_posts, sig.attention_z,
                sig.velocity, sig.engagement, sig.breadth, sig.stance,
                sig.composite, sig.confidence,
                json.dumps(sig.components, default=str),
            ),
        )
        new_id = int(cur.lastrowid or 0)
        await cur.close()
        await db.commit()
    return new_id


def _signal_from_row(row: aiosqlite.Row) -> MarketSignal:
    def _loads(v: Any) -> Any:
        if not v:
            return []
        try:
            return json.loads(str(v))
        except (ValueError, TypeError):
            return []

    comp = _loads(row["components_json"])
    return MarketSignal(
        market_id=str(row["market_id"]),
        ts=parse_iso(str(row["ts"])),
        category=(str(row["category"]) if row["category"] else None),
        provider=str(row["provider"]),
        topics=[str(t) for t in _loads(row["topics_json"])],
        matched_terms=[str(t) for t in _loads(row["matched_terms_json"])],
        n_posts=int(row["n_posts"]),
        n_matched_posts=int(row["n_matched_posts"]),
        attention_z=float(row["attention_z"]),
        velocity=float(row["velocity"]),
        engagement=float(row["engagement"]),
        breadth=float(row["breadth"]),
        stance=float(row["stance"]),
        composite=float(row["composite"]),
        confidence=float(row["confidence"]),
        components=comp if isinstance(comp, dict) else {},
    )


async def latest_market_signal(
    db_path: Path,
    market_id: str,
    *,
    max_age_hours: float = 24.0,
    now: datetime | None = None,
) -> MarketSignal | None:
    """Freshest signal row for a market, or None if absent/stale."""
    if not db_path.exists():
        return None
    now = now or now_utc()
    cutoff = iso(now - timedelta(hours=max_age_hours))
    try:
        async with aiosqlite.connect(str(db_path)) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM market_signals "
                "WHERE market_id = ? AND ts >= ? "
                "ORDER BY ts DESC LIMIT 1",
                (market_id, cutoff),
            ) as cur:
                row = await cur.fetchone()
    except aiosqlite.OperationalError:
        return None
    if row is None:
        return None
    try:
        return _signal_from_row(row)
    except (ValueError, TypeError) as exc:
        log.warning("signals: bad market_signals row for %s: %s", market_id, exc)
        return None


async def recent_market_signals(
    db_path: Path, *, limit: int = 12
) -> list[dict[str, Any]]:
    """Newest signal rows joined with market questions — dashboard feed."""
    if not db_path.exists():
        return []
    try:
        async with aiosqlite.connect(str(db_path)) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT s.ts, s.market_id, s.category, s.provider, "
                "s.n_posts, s.n_matched_posts, s.attention_z, s.velocity, "
                "s.stance, s.composite, s.confidence, m.question "
                "FROM market_signals s "
                "LEFT JOIN markets m ON m.id = s.market_id "
                "ORDER BY s.ts DESC LIMIT ?",
                (limit,),
            ) as cur:
                rows = await cur.fetchall()
    except aiosqlite.OperationalError:
        return []
    return [dict(r) for r in rows]


# ── signal building ──────────────────────────────────────


async def build_market_signal(
    db_path: Path,
    *,
    link: MarketTopicLink,
    posts_by_topic: dict[str, list[ConversationPost]],
    now: datetime,
    cfg: Config,
    provider_name: str,
) -> MarketSignal | None:
    """Score one market from already-fetched topic posts. None if the
    market links to no topic that produced posts this tick."""
    window_h = cfg.signals.window_hours
    all_posts: list[ConversationPost] = []
    used_topics: list[str] = []
    z_parts: list[float] = []
    vel_parts: list[float] = []
    baseline_points = 0
    for topic in link.topics:
        posts = posts_by_topic.get(topic)
        if posts is None:
            continue
        used_topics.append(topic)
        all_posts.extend(posts)
        activity = activity_from_posts(
            posts, source=provider_name, topic=topic,
            now=now, window_hours=window_h,
        )
        history = await topic_history(
            db_path, source=provider_name, topic=topic, before=now,
        )
        baseline_points = max(baseline_points, len(history))
        z_parts.append(
            attention_zscore(float(activity.n_posts), [float(h[0]) for h in history])
        )
        vel_parts.append(
            velocity_ratio(activity.posts_per_hour, [h[1] for h in history])
        )
    if not used_topics:
        return None

    matched = match_posts(all_posts, link.terms)
    community_fallback = not matched
    focus = matched if matched else all_posts

    n_focus_comments = sum(max(0, p.num_comments) for p in focus)
    focus_authors = len({p.author for p in focus if p.author})
    attention_z = max(z_parts) if z_parts else 0.0
    velocity = max(vel_parts) if vel_parts else 1.0
    engagement = engagement_norm(len(focus), n_focus_comments)
    breadth = breadth_norm(len(focus), focus_authors)
    stance = stance_of_posts(focus)

    composite, components = compose_conviction(
        attention_z=attention_z, velocity=velocity,
        engagement=engagement, breadth=breadth,
    )
    confidence = signal_confidence(
        n_matched_posts=(len(matched) if matched else len(all_posts)),
        baseline_points=baseline_points,
        min_matched_posts=cfg.signals.min_matched_posts,
        min_baseline_points=cfg.signals.min_baseline_snapshots,
        community_fallback=community_fallback,
    )
    components["community_fallback"] = community_fallback
    return MarketSignal(
        market_id=link.market_id,
        ts=now,
        category=link.category,
        provider=provider_name,
        topics=used_topics,
        matched_terms=list(link.terms),
        n_posts=len(all_posts),
        n_matched_posts=len(matched),
        attention_z=attention_z,
        velocity=velocity,
        engagement=engagement,
        breadth=breadth,
        stance=stance,
        composite=composite,
        confidence=confidence,
        components=components,
    )


async def _candidate_markets(
    db_path: Path, *, categories: set[str], limit: int
) -> list[Market]:
    """Unresolved markets in linkable categories, busiest first."""
    if not db_path.exists() or not categories:
        return []
    from polysim.db import dao

    placeholders = ",".join("?" for _ in categories)
    try:
        async with aiosqlite.connect(str(db_path)) as db, db.execute(
            "SELECT id FROM markets "
            f"WHERE resolved_outcome IS NULL AND category IN ({placeholders}) "
            "ORDER BY COALESCE(daily_volume_usd_cents, 0) DESC LIMIT ?",
            (*sorted(categories), limit),
        ) as cur:
            rows = await cur.fetchall()
    except aiosqlite.OperationalError:
        return []
    out: list[Market] = []
    for r in rows:
        m = await dao.get_market(db_path, str(r[0]))
        if m is not None:
            out.append(m)
    return out


async def run_snapshot_once(
    db_path: Path,
    cfg: Config,
    provider: ConversationProvider,
    *,
    now: datetime | None = None,
    markets: list[Market] | None = None,
) -> dict[str, int]:
    """One full pass: fetch every linkable topic once, snapshot activity,
    score every candidate market. Returns counters for logging/CLI."""
    now = now or now_utc()
    cat_topics = category_topics_from_config(cfg)
    if markets is None:
        markets = await _candidate_markets(
            db_path,
            categories=set(cat_topics),
            limit=cfg.signals.max_markets_per_snapshot,
        )
    links = [link_market(m, cat_topics) for m in markets]
    needed_topics = sorted({t for lk in links for t in lk.topics})

    posts_by_topic: dict[str, list[ConversationPost]] = {}
    topics_failed = 0
    for topic in needed_topics:
        posts = await provider.fetch_posts(topic)
        if posts is None:
            topics_failed += 1
            continue
        posts_by_topic[topic] = posts
        activity = activity_from_posts(
            posts, source=provider.name, topic=topic,
            now=now, window_hours=cfg.signals.window_hours,
        )
        await write_topic_snapshot(db_path, activity)

    signals_written = 0
    for lk in links:
        if not lk.linked:
            continue
        try:
            sig = await build_market_signal(
                db_path, link=lk, posts_by_topic=posts_by_topic,
                now=now, cfg=cfg, provider_name=provider.name,
            )
        except Exception as exc:
            log.warning("signals: scoring %s failed: %s", lk.market_id, exc)
            continue
        if sig is None:
            continue
        await write_market_signal(db_path, sig)
        signals_written += 1

    result = {
        "markets_considered": len(links),
        "topics_fetched": len(posts_by_topic),
        "topics_failed": topics_failed,
        "signals_written": signals_written,
    }
    log.info(
        "signals: snapshot — %d topics ok, %d failed, %d market signals",
        result["topics_fetched"], topics_failed, signals_written,
    )
    return result


def format_signal_note(sig: MarketSignal) -> str:
    """One-line summary for the investigator prompt / operator surfaces."""
    subs = ", ".join(f"r/{t}" for t in sig.topics[:3]) or "?"
    return (
        f"attention z={sig.attention_z:+.1f}, velocity {sig.velocity:.1f}x, "
        f"stance {sig.stance:+.2f}, {sig.n_matched_posts}/{sig.n_posts} posts "
        f"matched ({subs}; conviction {sig.composite:.2f}, "
        f"confidence {sig.confidence:.2f})"
    )


# ── optional live loop ───────────────────────────────────


class SignalSnapshotLoop:
    """Periodic run_snapshot_once. Off unless both the LiveConfig flag and
    config.signals.enabled are set — same start/stop shape as every other
    loop in live.py."""

    def __init__(
        self,
        db_path: Path,
        cfg: Config,
        provider: ConversationProvider,
        *,
        interval_s: float | None = None,
    ) -> None:
        self._db = db_path
        self._cfg = cfg
        self._provider = provider
        self._interval = interval_s or cfg.signals.snapshot_interval_s
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.snapshots_run = 0
        self.signals_written_total = 0

    async def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="signal-snapshot")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                r = await run_snapshot_once(self._db, self._cfg, self._provider)
                self.snapshots_run += 1
                self.signals_written_total += r.get("signals_written", 0)
            except Exception as exc:
                log.warning("signals: snapshot tick failed: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
                return
            except TimeoutError:
                continue


def provider_from_config(cfg: Config) -> ConversationProvider | None:
    """Build the configured provider; None when signals are off/none."""
    if not cfg.signals.enabled or cfg.signals.provider == "none":
        return None
    if cfg.signals.provider == "fixture":
        if not cfg.signals.fixtures_dir:
            log.warning("signals: provider=fixture but fixtures_dir unset")
            return None
        from polysim.signals.providers import FixtureProvider
        return FixtureProvider(Path(cfg.signals.fixtures_dir))
    from polysim.signals.providers import RedditPublicProvider
    return RedditPublicProvider(timeout_s=cfg.signals.request_timeout_s)


__all__ = [
    "SignalSnapshotLoop",
    "build_market_signal",
    "format_signal_note",
    "latest_market_signal",
    "provider_from_config",
    "recent_market_signals",
    "run_snapshot_once",
    "topic_history",
    "write_market_signal",
    "write_topic_snapshot",
]
