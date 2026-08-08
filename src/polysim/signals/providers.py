"""Conversation providers — pluggable post sources.

Three implementations:

  RedditPublicProvider  reddit.com's public JSON listing. Free and
                        credential-less, but 403s from datacenter IPs
                        (known from the equity lane), so every failure
                        degrades to None — the signal layer then simply
                        produces nothing for that topic this tick.
  FixtureProvider       reads posts from JSON files on disk. Used for
                        tests, backtests, and offline development.
  StaticProvider        in-memory mapping. Test harness convenience.

Contract: `fetch_posts` returns
  * list[ConversationPost]  — fetch succeeded (possibly empty)
  * None                    — fetch failed (network, HTTP, parse); caller
                              must treat this as "no data", never as
                              "zero activity".
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import httpx

from polysim.signals.schema import ConversationPost

log = logging.getLogger(__name__)

_REDDIT_UA = (
    "polysim/0.1 (paper-trading research; public conversation metrics; "
    "contact /u/polysim_operator)"
)


def normalize_topic(topic: str) -> str:
    """'r/boxoffice' / '/r/BoxOffice' / 'boxoffice' → 'boxoffice'."""
    t = topic.strip().lstrip("/").lower()
    return t.removeprefix("r/")


class ConversationProvider(Protocol):
    """Anything that can produce recent posts for a topic."""

    @property
    def name(self) -> str: ...

    async def fetch_posts(
        self, topic: str, *, limit: int = 75
    ) -> list[ConversationPost] | None: ...


def _post_from_reddit_child(topic: str, d: dict[str, Any]) -> ConversationPost | None:
    created_ts = float(d.get("created_utc") or 0.0)
    if created_ts <= 0:
        return None
    ext_id = str(d.get("id") or "")
    title = str(d.get("title") or "").strip()
    if not ext_id or not title:
        return None
    author = str(d.get("author") or "").strip() or None
    return ConversationPost(
        source="reddit",
        topic=topic,
        external_id=ext_id,
        created_at=datetime.fromtimestamp(created_ts, tz=UTC),
        title=title,
        text=str(d.get("selftext") or "").strip(),
        author=author,
        score=int(d.get("score") or 0),
        num_comments=int(d.get("num_comments") or 0),
        url=str(d.get("url") or "") or None,
    )


class RedditPublicProvider:
    """Public /r/<sub>/new.json listing — no credentials, best-effort."""

    def __init__(
        self,
        *,
        timeout_s: float = 10.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._timeout = timeout_s
        self._client = client

    @property
    def name(self) -> str:
        return "reddit"

    async def fetch_posts(
        self, topic: str, *, limit: int = 75
    ) -> list[ConversationPost] | None:
        sub = normalize_topic(topic)
        url = f"https://www.reddit.com/r/{sub}/new.json"
        owned = self._client is None
        client = self._client or httpx.AsyncClient(
            timeout=self._timeout, headers={"User-Agent": _REDDIT_UA},
        )
        try:
            r = await client.get(url, params={"limit": min(100, max(1, limit))})
            if r.status_code != 200:
                log.warning("signals: reddit GET r/%s → http %s", sub, r.status_code)
                return None
            children = (r.json().get("data") or {}).get("children") or []
        except Exception as exc:
            log.warning("signals: reddit fetch r/%s failed: %s", sub, exc)
            return None
        finally:
            if owned:
                await client.aclose()

        posts: list[ConversationPost] = []
        for ch in children:
            d = (ch or {}).get("data") or {}
            if not isinstance(d, dict):
                continue
            try:
                p = _post_from_reddit_child(sub, d)
            except (TypeError, ValueError):
                continue
            if p is not None:
                posts.append(p)
        posts.sort(key=lambda p: p.created_at, reverse=True)
        return posts


class FixtureProvider:
    """Reads `<root>/<topic>.json` — a JSON list of post dicts.

    Each dict needs: external_id, created_at (ISO), title; optional:
    text, author, score, num_comments, url. Missing file → None
    (mirrors a failed live fetch, so degradation paths are testable).
    """

    def __init__(self, root: Path) -> None:
        self._root = root

    @property
    def name(self) -> str:
        return "fixture"

    async def fetch_posts(
        self, topic: str, *, limit: int = 75
    ) -> list[ConversationPost] | None:
        sub = normalize_topic(topic)
        path = self._root / f"{sub}.json"
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("signals: fixture %s unreadable: %s", path, exc)
            return None
        if not isinstance(raw, list):
            return None
        posts: list[ConversationPost] = []
        for item in raw[:limit]:
            if not isinstance(item, dict):
                continue
            try:
                posts.append(ConversationPost(
                    source="reddit",
                    topic=sub,
                    external_id=str(item["external_id"]),
                    created_at=datetime.fromisoformat(str(item["created_at"])),
                    title=str(item["title"]),
                    text=str(item.get("text") or ""),
                    author=(str(item["author"]) if item.get("author") else None),
                    score=int(item.get("score") or 0),
                    num_comments=int(item.get("num_comments") or 0),
                    url=(str(item["url"]) if item.get("url") else None),
                ))
            except (KeyError, TypeError, ValueError) as exc:
                log.warning("signals: fixture %s bad row: %s", path, exc)
        posts.sort(key=lambda p: p.created_at, reverse=True)
        return posts


class StaticProvider:
    """In-memory provider for unit tests."""

    def __init__(self, mapping: dict[str, list[ConversationPost]]) -> None:
        self._map = {normalize_topic(k): v for k, v in mapping.items()}

    @property
    def name(self) -> str:
        return "static"

    async def fetch_posts(
        self, topic: str, *, limit: int = 75
    ) -> list[ConversationPost] | None:
        posts = self._map.get(normalize_topic(topic))
        if posts is None:
            return None
        return sorted(posts, key=lambda p: p.created_at, reverse=True)[:limit]


__all__ = [
    "ConversationProvider",
    "FixtureProvider",
    "RedditPublicProvider",
    "StaticProvider",
    "normalize_topic",
]
