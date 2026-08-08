"""Reddit intel scraper — parallel to Telegram's intel_channels.py.

Reddit's public JSON endpoint is free, unauthenticated, and generous
(~60 req/min per IP if you set a proper User-Agent). We pull
/r/<subreddit>/new.json, normalize each post into the same shape as
Telegram messages, and reuse:
  * intel_messages table (source='reddit:<sub>')
  * intel_extract for wallets/slugs/social_links
  * intel_llm.interpret for sentiment extraction
  * intel_wallet_matcher to close the loop

Post text = `title + "\\n\\n" + selftext`. Embedded links + OP comments
outside selftext aren't pulled (simpler = more reliable). Link-only
posts still carry useful signal via their title.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from polysim.db import dao
from polysim.ingest.intel_extract import extract_all
from polysim.ingest.twitter_resolver import (
    TweetCache,
    extract_tweet_ids,
    resolve_many,
)
from polysim.utils.time import iso, now_utc

log = logging.getLogger(__name__)

_REDDIT_UA = (
    "polysim/0.1 (by /u/polysim_operator; "
    "prediction-market insider detection research)"
)


def _source_name(subreddit: str) -> str:
    """Normalize 'r/boxoffice' / '/r/boxoffice' / 'boxoffice' → 'reddit:boxoffice'."""
    s = subreddit.strip().lstrip("/").lower()
    if s.startswith("r/"):
        s = s[2:]
    return f"reddit:{s}"


async def _fetch_new_posts(
    client: httpx.AsyncClient, subreddit: str, *, limit: int = 50
) -> list[dict[str, Any]]:
    """Hit https://www.reddit.com/r/<sub>/new.json and normalize."""
    sub = subreddit.strip().lstrip("/").lower().removeprefix("r/")
    url = f"https://www.reddit.com/r/{sub}/new.json"
    r = await client.get(url, params={"limit": min(100, limit)})
    if r.status_code != 200:
        log.warning("reddit GET %s: http %s", url, r.status_code)
        return []
    children = (r.json().get("data") or {}).get("children") or []
    out: list[dict[str, Any]] = []
    for ch in children:
        d = (ch or {}).get("data") or {}
        if not isinstance(d, dict):
            continue
        created_ts = float(d.get("created_utc") or 0.0)
        posted = (
            datetime.fromtimestamp(created_ts, tz=UTC).isoformat()
            if created_ts > 0 else iso(now_utc())
        )
        title = str(d.get("title") or "").strip()
        selftext = str(d.get("selftext") or "").strip()
        author = str(d.get("author") or "").strip() or None
        link = str(d.get("url") or "")
        # Compose a single text blob so the LLM + regex extractor work the
        # same way as on Telegram messages.
        text_parts = [title]
        if selftext:
            text_parts.append(selftext)
        if link and not link.startswith("https://www.reddit.com/"):
            text_parts.append(f"Link: {link}")
        text = "\n\n".join(text_parts)
        if not text.strip():
            continue
        out.append({
            "external_id": str(d.get("id") or ""),
            "posted_at": posted,
            "author": author,
            "text": text,
            "raw": {
                "id": d.get("id"),
                "permalink": d.get("permalink"),
                "subreddit": d.get("subreddit"),
                "score": d.get("score"),
                "num_comments": d.get("num_comments"),
                "over_18": d.get("over_18"),
                "url": link,
            },
        })
    return out


async def sync_subreddit_once(
    db_path: Path,
    *,
    subreddit: str,
    keywords_by_category: dict[str, list[str]] | None = None,
    limit: int = 50,
    resolve_x_links: bool = True,
    client: httpx.AsyncClient | None = None,
    tweet_cache: TweetCache | None = None,
) -> dict[str, Any]:
    """One pull for one subreddit. Returns {new_messages, new_wallets,
    new_wallets_from_x, subreddit}."""
    source = _source_name(subreddit)
    tweet_cache = tweet_cache or TweetCache()

    owned_client = client is None
    if client is None:
        client = httpx.AsyncClient(
            timeout=20.0, headers={"User-Agent": _REDDIT_UA},
        )
    try:
        raw_posts = await _fetch_new_posts(client, subreddit, limit=limit)

        new_msgs = 0
        new_wallets = 0
        new_wallets_from_x = 0
        for m in raw_posts:
            intel = extract_all(m["text"], keywords_by_category=keywords_by_category)

            tweet_wallets: list[tuple[str, str, str]] = []
            if resolve_x_links:
                tweet_ids = extract_tweet_ids(m["text"])
                if tweet_ids:
                    resolved = await resolve_many(tweet_ids, cache=tweet_cache)
                    for tid, tw in resolved.items():
                        if tw is None:
                            continue
                        from polysim.ingest.intel_extract import extract_wallets
                        for w in extract_wallets(tw.text):
                            tweet_wallets.append((w, tid, tw.author or "?"))

            combined_wallets = list(intel.wallets)
            seen = set(intel.wallets)
            for w, _tid, _auth in tweet_wallets:
                if w not in seen:
                    seen.add(w)
                    combined_wallets.append(w)

            msg_id = await dao.write_intel_message(
                db_path,
                source=source,
                external_id=m["external_id"],
                posted_at=m["posted_at"],
                author=m["author"],
                text=m["text"],
                wallets=combined_wallets,
                market_slugs=intel.market_slugs,
                categories=intel.categories,
                raw=m.get("raw"),
            )
            if msg_id is None:
                continue
            new_msgs += 1

            for w in intel.wallets:
                label = f"{source}:{w[:10]}"
                await dao.upsert_known_insider(
                    db_path,
                    label=label, address=w,
                    source=f"intel:{source}",
                    source_message_id=msg_id,
                    notes=(
                        f"Auto-ingested from r/{subreddit.strip().lstrip('/').removeprefix('r/')} "
                        f"post {m['external_id']}"
                    ),
                )
                new_wallets += 1

            for w, tid, auth in tweet_wallets:
                if w in intel.wallets:
                    continue
                label = f"{source}:x:{w[:10]}"
                await dao.upsert_known_insider(
                    db_path,
                    label=label, address=w,
                    source=f"intel:{source}->x:{auth}",
                    source_message_id=msg_id,
                    notes=(
                        f"Auto-ingested from r/{subreddit} post {m['external_id']} → "
                        f"x.com/{auth}/status/{tid}"
                    ),
                )
                new_wallets_from_x += 1

        return {
            "subreddit": subreddit,
            "new_messages": new_msgs,
            "new_wallets": new_wallets,
            "new_wallets_from_x": new_wallets_from_x,
        }
    finally:
        if owned_client:
            await client.aclose()


class RedditIntelPoller:
    """Long-running poller mirroring IntelPoller for Telegram."""

    def __init__(
        self,
        db_path: Path,
        *,
        subreddits: list[str],
        keywords_by_category: dict[str, list[str]] | None = None,
        poll_interval_s: float = 300.0,
    ) -> None:
        self._db = db_path
        self._subs = list(subreddits)
        self._keywords = keywords_by_category or {}
        self._poll = poll_interval_s
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.totals: dict[str, int] = {"messages": 0, "wallets": 0}

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="reddit-intel-poll")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        async with httpx.AsyncClient(
            timeout=20.0, headers={"User-Agent": _REDDIT_UA},
        ) as client:
            while not self._stop.is_set():
                for sub in self._subs:
                    try:
                        r = await sync_subreddit_once(
                            self._db, subreddit=sub,
                            keywords_by_category=self._keywords,
                            client=client,
                        )
                        self.totals["messages"] += int(r.get("new_messages") or 0)
                        self.totals["wallets"] += int(r.get("new_wallets") or 0)
                        if r.get("new_messages"):
                            log.info(
                                "reddit r/%s: +%d msgs",
                                sub, r["new_messages"],
                            )
                    except Exception as exc:
                        log.warning("reddit sync r/%s failed: %s", sub, exc)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self._poll)
                    return
                except TimeoutError:
                    continue
