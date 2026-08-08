"""Provider tests — Reddit parsing via httpx.MockTransport (no network),
fixture reading, and the None-on-failure degradation contract."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx

from polysim.signals.providers import (
    FixtureProvider,
    RedditPublicProvider,
    StaticProvider,
    normalize_topic,
)
from polysim.signals.schema import ConversationPost


def test_normalize_topic_variants() -> None:
    assert normalize_topic("r/BoxOffice") == "boxoffice"
    assert normalize_topic("/r/boxoffice") == "boxoffice"
    assert normalize_topic("boxoffice") == "boxoffice"


def _reddit_payload() -> dict:
    return {
        "data": {
            "children": [
                {"data": {
                    "id": "abc", "created_utc": 1_782_000_000,
                    "title": "Dune tracking thread", "selftext": "big numbers",
                    "author": "moviefan", "score": 120, "num_comments": 45,
                    "url": "https://reddit.com/x",
                }},
                {"data": {
                    "id": "", "created_utc": 1_782_000_100, "title": "no id",
                }},
                {"data": {
                    "id": "def", "created_utc": 0, "title": "no timestamp",
                }},
            ],
        },
    }


async def test_reddit_provider_parses_and_filters() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "boxoffice/new.json" in str(request.url)
        return httpx.Response(200, json=_reddit_payload())

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = RedditPublicProvider(client=client)
    posts = await provider.fetch_posts("r/boxoffice")
    await client.aclose()

    assert posts is not None
    assert len(posts) == 1  # rows without id/timestamp dropped
    p = posts[0]
    assert p.external_id == "abc"
    assert p.topic == "boxoffice"
    assert p.num_comments == 45
    assert p.created_at.tzinfo is not None


async def test_reddit_provider_http_error_returns_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="blocked")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = RedditPublicProvider(client=client)
    assert await provider.fetch_posts("boxoffice") is None
    await client.aclose()


async def test_reddit_provider_network_error_returns_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = RedditPublicProvider(client=client)
    assert await provider.fetch_posts("boxoffice") is None
    await client.aclose()


async def test_fixture_provider_roundtrip(tmp_path: Path) -> None:
    (tmp_path / "boxoffice.json").write_text(json.dumps([
        {
            "external_id": "f1",
            "created_at": "2026-07-01T10:00:00+00:00",
            "title": "Fixture post",
            "text": "dune part three",
            "author": "u1",
            "score": 10,
            "num_comments": 3,
        },
        {"bad": "row"},
    ]), encoding="utf-8")
    provider = FixtureProvider(tmp_path)
    posts = await provider.fetch_posts("r/boxoffice")
    assert posts is not None
    assert len(posts) == 1
    assert posts[0].external_id == "f1"


async def test_fixture_provider_missing_file_is_none(tmp_path: Path) -> None:
    provider = FixtureProvider(tmp_path)
    assert await provider.fetch_posts("nosuchsub") is None


async def test_static_provider_normalizes_keys() -> None:
    p = ConversationPost(
        source="reddit", topic="boxoffice", external_id="1",
        created_at=datetime(2026, 7, 1, tzinfo=UTC), title="t",
    )
    provider = StaticProvider({"r/BoxOffice": [p]})
    posts = await provider.fetch_posts("boxoffice")
    assert posts is not None and len(posts) == 1
    assert await provider.fetch_posts("unknown") is None
