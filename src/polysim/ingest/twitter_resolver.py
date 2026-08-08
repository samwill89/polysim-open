"""Tier-4 X/Twitter resolver — authless text fetcher.

Hits `cdn.syndication.twimg.com/tweet-result` (the endpoint X uses for
embed widgets). No account needed, no token scraping, no ban risk.

Limitations we accept:
  * Text capped at ~280 characters — long-form `note_tweet` content
    returns only an opaque reference id, not the text.
  * Single tweet at a time — threads return only the top tweet. The JSON
    does carry `conversation_count` so callers know there's more.
  * Rate-limited at ~50 req/min per IP before Cloudflare 429s.

Returns None on any failure — callers decide whether to continue.
"""

from __future__ import annotations

import logging
import re
import secrets
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger(__name__)

_SYNDICATION_URL = "https://cdn.syndication.twimg.com/tweet-result"

# x.com / twitter.com /<user>/status/<id> — id is numeric.
_TWEET_URL_RE = re.compile(
    r"https?://(?:www\.|mobile\.)?(?:x|twitter)\.com/"
    r"[A-Za-z0-9_]+/status(?:es)?/(\d+)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ResolvedTweet:
    id: str
    author: str | None
    text: str
    conversation_count: int            # replies in thread; 0 if single
    truncated: bool                    # True if note_tweet id was present
    created_at: str | None


def extract_tweet_ids(text: str) -> list[str]:
    """Find every x.com/twitter.com status id in `text`, de-duped."""
    seen: set[str] = set()
    out: list[str] = []
    for m in _TWEET_URL_RE.finditer(text or ""):
        tid = m.group(1)
        if tid and tid not in seen:
            seen.add(tid)
            out.append(tid)
    return out


async def resolve_tweet(
    tweet_id: str,
    *,
    client: httpx.AsyncClient | None = None,
    timeout_s: float = 10.0,
) -> ResolvedTweet | None:
    """Fetch one tweet by id. Returns None on error."""
    if not tweet_id.isdigit():
        return None
    token = secrets.token_hex(8)
    params = {"id": tweet_id, "token": token, "lang": "en"}
    headers = {"User-Agent": "Mozilla/5.0 PolySim/0.1"}
    owned = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=timeout_s)
    try:
        r = await client.get(_SYNDICATION_URL, params=params, headers=headers)
        if r.status_code != 200:
            log.debug("tweet %s: http %s", tweet_id, r.status_code)
            return None
        return _parse_tweet_json(r.json())
    except Exception as exc:
        log.warning("resolve_tweet %s failed: %s", tweet_id, type(exc).__name__)
        return None
    finally:
        if owned:
            await client.aclose()


def _parse_tweet_json(data: Any) -> ResolvedTweet | None:
    if not isinstance(data, dict):
        return None
    tid = data.get("id_str") or ""
    if not tid:
        return None
    user = data.get("user") or {}
    author = user.get("screen_name") if isinstance(user, dict) else None
    text = data.get("text") or ""
    # note_tweet presence => long-form content exists that we can't fetch
    truncated = bool(data.get("note_tweet"))
    convo = int(data.get("conversation_count") or 0)
    created = data.get("created_at")
    return ResolvedTweet(
        id=str(tid),
        author=str(author) if author else None,
        text=str(text),
        conversation_count=convo,
        truncated=truncated,
        created_at=str(created) if created else None,
    )


class TweetCache:
    """In-memory LRU-ish cache to avoid re-fetching the same tweet.

    The scraper revisits messages and walks the same linked tweets many
    times. Cache keyed by tweet id, cleared manually or on process exit.
    """

    def __init__(self, capacity: int = 2_000) -> None:
        self._capacity = capacity
        self._data: dict[str, ResolvedTweet | None] = {}

    def get(self, tweet_id: str) -> ResolvedTweet | None | object:
        """Returns the cached result, or the sentinel `MISS` if not cached."""
        return self._data.get(tweet_id, _MISS)

    def put(self, tweet_id: str, value: ResolvedTweet | None) -> None:
        if len(self._data) >= self._capacity:
            # drop arbitrary 20%
            for k in list(self._data.keys())[: self._capacity // 5]:
                del self._data[k]
        self._data[tweet_id] = value

    def __len__(self) -> int:
        return len(self._data)


_MISS = object()


async def resolve_many(
    tweet_ids: list[str],
    *,
    cache: TweetCache | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict[str, ResolvedTweet | None]:
    """Resolve a batch of tweet ids; uses cache + a shared httpx client."""
    out: dict[str, ResolvedTweet | None] = {}
    # Can't use `cache or TweetCache()` — empty TweetCache is falsy (len==0).
    if cache is None:
        cache = TweetCache()
    owned_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=10.0)
    try:
        for tid in tweet_ids:
            cached = cache.get(tid)
            if cached is not _MISS:
                out[tid] = cached  # type: ignore[assignment]
                continue
            t = await resolve_tweet(tid, client=client)
            cache.put(tid, t)
            out[tid] = t
    finally:
        if owned_client:
            await client.aclose()
    return out


__all__ = [
    "ResolvedTweet",
    "TweetCache",
    "extract_tweet_ids",
    "resolve_many",
    "resolve_tweet",
]
