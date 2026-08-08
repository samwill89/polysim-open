from __future__ import annotations

import json

import httpx
import pytest

from polysim.risk_intelligence.providers import (
    FallbackEvidenceProvider,
    GDELTProvider,
    GoogleNewsRSSProvider,
    StaticEvidenceProvider,
)
from polysim.risk_intelligence.schema import EvidenceArticle


@pytest.mark.asyncio
async def test_gdelt_provider_normalizes_and_deduplicates() -> None:
    payload = {
        "articles": [
            {
                "title": "McConnell update",
                "url": "https://www.apnews.com/story",
                "domain": "www.apnews.com",
                "seendate": "20260710T210000Z",
                "language": "English",
                "sourcecountry": "United States",
            },
            {
                "title": "McConnell update",
                "url": "https://www.apnews.com/duplicate",
                "domain": "www.apnews.com",
                "seendate": "20260710T210000Z",
            },
            {"title": "bad row", "url": "", "seendate": "bad"},
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["mode"] == "artlist"
        assert request.url.params["query"] == (
            '"Mitch McConnell" ("resign" OR "step down")'
        )
        return httpx.Response(200, content=json.dumps(payload).encode())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GDELTProvider(client=client)
        rows = await provider.fetch_articles(
            "Mitch McConnell",
            keywords=("resign", "step down"),
        )

    assert rows is not None
    assert len(rows) == 1
    assert rows[0].domain == "apnews.com"
    assert rows[0].published_at.year == 2026


@pytest.mark.asyncio
async def test_gdelt_provider_returns_none_on_http_failure() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GDELTProvider(client=client)
        assert await provider.fetch_articles("Mitch McConnell") is None


@pytest.mark.asyncio
async def test_gdelt_provider_retries_one_rate_limit() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        del request
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"retry-after": "0"})
        return httpx.Response(200, json={"articles": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GDELTProvider(client=client)
        assert await provider.fetch_articles("Mitch McConnell") == []
    assert calls == 2


@pytest.mark.asyncio
async def test_google_news_rss_retains_publisher_domain() -> None:
    xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <rss><channel><item>
      <title>McConnell confirms resignation plan - Reuters</title>
      <link>https://news.google.com/rss/articles/example</link>
      <pubDate>Fri, 10 Jul 2026 21:00:00 GMT</pubDate>
      <source url="https://www.reuters.com">Reuters</source>
    </item></channel></rss>"""

    def handler(request: httpx.Request) -> httpx.Response:
        assert '"Mitch McConnell"' in request.url.params["q"]
        assert '"resign"' in request.url.params["q"]
        assert "when:7d" in request.url.params["q"]
        return httpx.Response(200, content=xml)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GoogleNewsRSSProvider(client=client)
        rows = await provider.fetch_articles("Mitch McConnell", keywords=("resign",))
    assert rows is not None
    assert len(rows) == 1
    assert rows[0].domain == "reuters.com"
    assert rows[0].published_at.year == 2026


@pytest.mark.asyncio
async def test_fallback_provider_advances_only_after_failure() -> None:
    article = EvidenceArticle(
        title="McConnell resignation update",
        url="https://reuters.com/story",
        domain="reuters.com",
        published_at="2026-07-10T21:00:00Z",
    )
    first = StaticEvidenceProvider({"mitch mcconnell": None})
    second = StaticEvidenceProvider({"mitch mcconnell": [article]})
    provider = FallbackEvidenceProvider((first, second))
    rows = await provider.fetch_articles("Mitch McConnell")
    assert rows == [article]
    assert first.calls == 1
    assert second.calls == 1
