"""Tier-4 Twitter resolver — authless syndication endpoint.

Uses respx to stub cdn.syndication.twimg.com responses so tests stay
offline.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from polysim.ingest.twitter_resolver import (
    TweetCache,
    _parse_tweet_json,
    extract_tweet_ids,
    resolve_many,
    resolve_tweet,
)


def test_extract_tweet_ids_from_mixed_text() -> None:
    text = (
        "check https://x.com/spacexbt/status/2036428209458135127 and "
        "https://twitter.com/DeItaone/status/2030752758945521841 "
        "and https://mobile.twitter.com/other/status/1 plus "
        "https://t.me/somechannel (not a tweet)"
    )
    ids = extract_tweet_ids(text)
    assert ids == [
        "2036428209458135127",
        "2030752758945521841",
        "1",
    ]


def test_parse_tweet_json_extracts_core_fields() -> None:
    sample = {
        "id_str": "123",
        "text": "whale 0xaF4d02c1B5e882F0a7d3c4E5f6a7b8C900110000 entered",
        "user": {"screen_name": "spacexbt"},
        "conversation_count": 3,
        "note_tweet": {"id": "abc"},
        "created_at": "2026-03-24T13:01:34.000Z",
    }
    t = _parse_tweet_json(sample)
    assert t is not None
    assert t.id == "123"
    assert t.author == "spacexbt"
    assert "whale" in t.text
    assert t.truncated is True
    assert t.conversation_count == 3


def test_parse_tweet_json_returns_none_for_bad_input() -> None:
    assert _parse_tweet_json(None) is None
    assert _parse_tweet_json({"no_id": True}) is None
    assert _parse_tweet_json([]) is None


@respx.mock
async def test_resolve_tweet_happy_path() -> None:
    respx.get("https://cdn.syndication.twimg.com/tweet-result").mock(
        return_value=httpx.Response(
            200,
            json={
                "id_str": "42",
                "text": "hello 0xaF4d02c1B5e882F0a7d3c4E5f6a7b8C900110000 world",
                "user": {"screen_name": "somebody"},
                "conversation_count": 0,
            },
        )
    )
    t = await resolve_tweet("42")
    assert t is not None
    assert t.id == "42"
    assert t.author == "somebody"


@respx.mock
async def test_resolve_tweet_404_returns_none() -> None:
    respx.get("https://cdn.syndication.twimg.com/tweet-result").mock(
        return_value=httpx.Response(404, json={})
    )
    assert await resolve_tweet("999") is None


async def test_resolve_tweet_rejects_non_numeric_id() -> None:
    assert await resolve_tweet("not-a-tweet-id") is None
    assert await resolve_tweet("") is None


@respx.mock
async def test_resolve_many_uses_cache() -> None:
    route = respx.get("https://cdn.syndication.twimg.com/tweet-result").mock(
        return_value=httpx.Response(
            200,
            json={
                "id_str": "1",
                "text": "hi",
                "user": {"screen_name": "a"},
                "conversation_count": 0,
            },
        )
    )
    cache = TweetCache()
    await resolve_many(["1"], cache=cache)
    await resolve_many(["1"], cache=cache)
    # Only one upstream call — second trip hits cache.
    assert route.call_count == 1


def test_cache_bounded() -> None:
    cache = TweetCache(capacity=10)
    for i in range(25):
        cache.put(str(i), None)
    assert len(cache) <= 10


@pytest.mark.integration
async def test_live_syndication_endpoint() -> None:
    """Skipped in quick runs — hits the real cdn.syndication.twimg.com."""
    # Tweet id that resolved at the time of writing. If it ever 404s, skip.
    t = await resolve_tweet("2036428209458135127")
    if t is None:
        pytest.skip("syndication endpoint unreachable or tweet gone")
    assert t.author is not None
    assert len(t.text) > 0
