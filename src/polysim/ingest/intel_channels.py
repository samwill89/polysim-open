"""Telegram channel intel poller.

Uses the operator's user account (via Telethon / MTProto) to read public
channels like @spaceinsights. For each new message:

  1. Extract wallets / market slugs / category keywords.
  2. Persist to `intel_messages`.
  3. Upsert any extracted wallets into `known_insiders` with source="intel:<channel>".
  4. Optionally push a Telegram alert back to the operator's bot chat.

The session file at ~/.polysim/telegram.session is created by
`scripts/telegram_user_auth.py` — this module never prompts for a login.

Responsibilities split for testability:
  * `sync_channel_once(client, ...)` does a single pull (fixture-testable
    via a fake client).
  * `IntelPoller` wraps it in an asyncio loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from polysim.db import dao
from polysim.ingest.intel_extract import extract_all, extract_wallets
from polysim.ingest.twitter_resolver import (
    TweetCache,
    extract_tweet_ids,
    resolve_many,
)
from polysim.utils.time import iso, now_utc

log = logging.getLogger(__name__)

SESSION_PATH = Path.home() / ".polysim" / "telegram.session"


async def _iter_new_messages(
    client: Any,  # telethon.TelegramClient, but typed Any to avoid import in tests
    channel: str,
    *,
    min_id: int = 0,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Return messages from `channel` with id > min_id, newest-first.

    Each entry is a plain dict with keys: external_id, posted_at, author, text, raw.
    """
    out: list[dict[str, Any]] = []
    entity = await client.get_entity(channel)
    # Telethon's iter_messages yields newest-first by default; `min_id`
    # filters them server-side.
    async for msg in client.iter_messages(entity, limit=limit, min_id=min_id):
        text = msg.message or ""
        if not text.strip():
            continue
        author = None
        sender = await msg.get_sender() if msg.sender_id is not None else None
        if sender is not None:
            author = getattr(sender, "username", None) or str(getattr(sender, "id", ""))
        posted = msg.date.isoformat() if msg.date else iso(now_utc())
        out.append({
            "external_id": str(msg.id),
            "posted_at": posted,
            "author": author,
            "text": text,
            "raw": {
                "id": msg.id,
                "chat": channel,
                "views": getattr(msg, "views", None),
                "forwards": getattr(msg, "forwards", None),
            },
        })
    return out


async def sync_channel_once(
    db_path: Path,
    client: Any,
    *,
    channel: str,
    keywords_by_category: dict[str, list[str]] | None = None,
    limit: int = 50,
    resolve_x_links: bool = True,
    tweet_cache: TweetCache | None = None,
) -> dict[str, int]:
    """One pull for one channel. Returns counters.

    Cursor: highest external_id already in `intel_messages` for this source.
    When `resolve_x_links=True`, walks x.com/twitter.com links in each
    message via the authless syndication endpoint and extracts wallets
    from the resolved tweet text (Tier-4).
    """
    source = _source_name(channel)
    min_id = (await dao.max_intel_external_id(db_path, source=source)) or 0
    raw_msgs = await _iter_new_messages(
        client, channel, min_id=min_id, limit=limit
    )

    tweet_cache = tweet_cache or TweetCache()
    new_msgs = 0
    new_wallets = 0
    new_wallets_from_x = 0
    for m in raw_msgs:
        intel = extract_all(m["text"], keywords_by_category=keywords_by_category)

        # Tier 4: resolve x.com/twitter.com links and merge wallets from
        # the linked tweets into this message's extracted wallet list.
        tweet_wallets: list[tuple[str, str, str]] = []  # (wallet, tweet_id, author)
        if resolve_x_links:
            tweet_ids = extract_tweet_ids(m["text"])
            if tweet_ids:
                resolved = await resolve_many(tweet_ids, cache=tweet_cache)
                for tid, tw in resolved.items():
                    if tw is None:
                        continue
                    for w in extract_wallets(tw.text):
                        tweet_wallets.append((w, tid, tw.author or "?"))

        # Wallets list persisted with the message is the union of inline
        # addresses and resolved-tweet addresses.
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

        # Inline-paste wallets.
        for w in intel.wallets:
            label = f"{source}:{w[:10]}"
            await dao.upsert_known_insider(
                db_path,
                label=label,
                address=w,
                source=f"intel:{source}",
                source_message_id=msg_id,
                notes=(
                    f"Auto-ingested from @{source} msg {m['external_id']}"
                    + (f" (categories: {', '.join(intel.categories)})" if intel.categories else "")
                ),
            )
            new_wallets += 1

        # Tier-4 wallets (from linked X posts).
        for w, tid, auth in tweet_wallets:
            if w in intel.wallets:
                continue  # already recorded inline
            label = f"{source}:x:{w[:10]}"
            await dao.upsert_known_insider(
                db_path,
                label=label,
                address=w,
                source=f"intel:{source}->x:{auth}",
                source_message_id=msg_id,
                notes=(
                    f"Auto-ingested from @{source} msg {m['external_id']} → "
                    f"x.com/{auth}/status/{tid}"
                ),
            )
            new_wallets_from_x += 1

    return {
        "channel": channel,  # type: ignore[dict-item]
        "new_messages": new_msgs,
        "new_wallets": new_wallets,
        "new_wallets_from_x": new_wallets_from_x,
    }


def _source_name(channel: str) -> str:
    """Normalize '@spaceinsights' / 'spaceinsights' / 't.me/spaceinsights' → 'spaceinsights'."""
    s = channel.strip().lstrip("@")
    if s.startswith("http://") or s.startswith("https://"):
        s = s.rstrip("/").rsplit("/", 1)[-1]
    if s.startswith("t.me/"):
        s = s[len("t.me/"):]
    return s.lower()


class IntelPoller:
    """Long-running: polls every `poll_interval_s`, calls sync_channel_once
    for each configured channel.

    Telethon client is built lazily inside start() so tests can drive
    sync_channel_once() directly without import-time requirement.
    """

    def __init__(
        self,
        db_path: Path,
        *,
        channels: Iterable[str],
        api_id: int,
        api_hash: str,
        keywords_by_category: dict[str, list[str]] | None = None,
        poll_interval_s: float = 300.0,
    ) -> None:
        self._db = db_path
        self._channels = [c for c in channels if c]
        self._api_id = api_id
        self._api_hash = api_hash
        self._keywords = keywords_by_category or {}
        self._poll = poll_interval_s
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.totals: dict[str, int] = {"messages": 0, "wallets": 0}

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="intel-poller")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        from telethon import TelegramClient

        client = TelegramClient(
            str(SESSION_PATH), self._api_id, self._api_hash
        )
        await client.connect()
        if not await client.is_user_authorized():
            log.error(
                "telegram session not authorized; run scripts/telegram_user_auth.py"
            )
            await client.disconnect()
            return
        try:
            while not self._stop.is_set():
                for ch in self._channels:
                    try:
                        result = await sync_channel_once(
                            self._db, client, channel=ch,
                            keywords_by_category=self._keywords,
                        )
                        self.totals["messages"] += int(result.get("new_messages") or 0)
                        self.totals["wallets"] += int(result.get("new_wallets") or 0)
                        if result.get("new_messages"):
                            log.info(
                                "intel %s: +%d msgs, +%d wallets",
                                ch, result["new_messages"], result["new_wallets"],
                            )
                    except Exception as exc:
                        log.warning("intel sync %s failed: %s", ch, exc)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self._poll)
                    return
                except TimeoutError:
                    continue
        finally:
            await client.disconnect()
