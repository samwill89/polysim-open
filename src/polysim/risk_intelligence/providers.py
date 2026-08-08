"""Public-news providers for evidence scans."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Protocol
from urllib.parse import urlparse
from xml.etree import ElementTree

import httpx

from polysim.risk_intelligence.schema import EvidenceArticle
from polysim.risk_intelligence.scoring import normalize_domain

log = logging.getLogger(__name__)


class EvidenceProvider(Protocol):
    @property
    def name(self) -> str: ...

    async def fetch_articles(
        self,
        subject: str,
        *,
        keywords: tuple[str, ...] = (),
        limit: int = 30,
        lookback_days: int = 7,
    ) -> list[EvidenceArticle] | None: ...


def _parse_gdelt_date(value: Any) -> datetime | None:
    text = str(value or "").strip()
    try:
        return datetime.strptime(text, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None


class GDELTProvider:
    """Credential-free GDELT DOC 2.0 ArticleList client."""

    endpoint = "https://api.gdeltproject.org/api/v2/doc/doc"

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
        return "gdelt"

    async def fetch_articles(
        self,
        subject: str,
        *,
        keywords: tuple[str, ...] = (),
        limit: int = 30,
        lookback_days: int = 7,
    ) -> list[EvidenceArticle] | None:
        subject = " ".join(subject.strip().split())
        if len(subject) < 3:
            return []
        owned = self._client is None
        client = self._client or httpx.AsyncClient(timeout=self._timeout)
        try:
            query = f'"{subject}"'
            clean_keywords = tuple(
                dict.fromkeys(" ".join(term.split()) for term in keywords if term.strip())
            )
            if clean_keywords:
                event_query = " OR ".join(f'"{term}"' for term in clean_keywords[:12])
                query = f"{query} ({event_query})"
            params: dict[str, str | int] = {
                "query": query,
                "mode": "artlist",
                "maxrecords": min(75, max(1, limit)),
                "timespan": f"{max(1, lookback_days)}d",
                "sort": "datedesc",
                "format": "json",
            }
            response = await client.get(self.endpoint, params=params)
            if response.status_code == 429:
                retry_after = response.headers.get("retry-after")
                try:
                    delay = 5.25 if retry_after is None else float(retry_after)
                    delay = min(10.0, max(0.25, delay))
                except ValueError:
                    delay = 5.25
                await asyncio.sleep(delay)
                response = await client.get(self.endpoint, params=params)
            response.raise_for_status()
            raw = response.json()
        except Exception as exc:
            log.warning(
                "evidence: GDELT fetch for %r failed: %s: %s",
                subject,
                type(exc).__name__,
                exc,
            )
            return None
        finally:
            if owned:
                await client.aclose()

        rows = raw.get("articles") if isinstance(raw, dict) else None
        if not isinstance(rows, list):
            return None
        articles: list[EvidenceArticle] = []
        seen: set[tuple[str, str]] = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            title = str(row.get("title") or "").strip()
            url = str(row.get("url") or "").strip()
            published = _parse_gdelt_date(row.get("seendate"))
            domain = normalize_domain(str(row.get("domain") or url))
            key = (domain, title.lower())
            if not title or not url or published is None or key in seen:
                continue
            seen.add(key)
            articles.append(
                EvidenceArticle(
                    title=title,
                    url=url,
                    domain=domain,
                    published_at=published,
                    language=(str(row["language"]) if row.get("language") else None),
                    source_country=(
                        str(row["sourcecountry"]) if row.get("sourcecountry") else None
                    ),
                )
            )
        return articles


class GoogleNewsRSSProvider:
    """Credential-free fallback retaining each publisher's source domain."""

    endpoint = "https://news.google.com/rss/search"

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
        return "google_news_rss"

    async def fetch_articles(
        self,
        subject: str,
        *,
        keywords: tuple[str, ...] = (),
        limit: int = 30,
        lookback_days: int = 7,
    ) -> list[EvidenceArticle] | None:
        subject = " ".join(subject.strip().split())
        if len(subject) < 3:
            return []
        clean_keywords = tuple(
            dict.fromkeys(" ".join(term.split()) for term in keywords if term.strip())
        )
        query = f'"{subject}"'
        if clean_keywords:
            event_query = " OR ".join(f'"{term}"' for term in clean_keywords[:6])
            query = f"{query} ({event_query})"
        query = f"{query} when:{max(1, lookback_days)}d"
        owned = self._client is None
        client = self._client or httpx.AsyncClient(timeout=self._timeout)
        try:
            response = await client.get(
                self.endpoint,
                params={"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"},
            )
            response.raise_for_status()
            root = ElementTree.fromstring(response.content)
        except Exception as exc:
            log.warning(
                "evidence: Google News RSS fetch for %r failed: %s: %s",
                subject,
                type(exc).__name__,
                exc,
            )
            return None
        finally:
            if owned:
                await client.aclose()

        articles: list[EvidenceArticle] = []
        seen: set[tuple[str, str]] = set()
        for item in root.findall(".//item"):
            title = (item.findtext("title") or "").strip()
            url = (item.findtext("link") or "").strip()
            source = item.find("source")
            source_url = str(source.attrib.get("url") or "") if source is not None else ""
            domain = normalize_domain(source_url or urlparse(url).netloc)
            try:
                published = parsedate_to_datetime(item.findtext("pubDate") or "")
                if published.tzinfo is None:
                    published = published.replace(tzinfo=UTC)
            except (TypeError, ValueError):
                continue
            key = (domain, title.lower())
            if not title or not url or not domain or key in seen:
                continue
            seen.add(key)
            articles.append(
                EvidenceArticle(
                    title=title,
                    url=url,
                    domain=domain,
                    published_at=published,
                    language="English",
                )
            )
            if len(articles) >= limit:
                break
        return articles


class FallbackEvidenceProvider:
    """Use the next provider only when the preceding provider failed."""

    def __init__(self, providers: tuple[EvidenceProvider, ...]) -> None:
        self._providers = providers

    @property
    def name(self) -> str:
        return "+".join(provider.name for provider in self._providers)

    async def fetch_articles(
        self,
        subject: str,
        *,
        keywords: tuple[str, ...] = (),
        limit: int = 30,
        lookback_days: int = 7,
    ) -> list[EvidenceArticle] | None:
        for provider in self._providers:
            rows = await provider.fetch_articles(
                subject,
                keywords=keywords,
                limit=limit,
                lookback_days=lookback_days,
            )
            if rows is not None:
                return rows
        return None


class StaticEvidenceProvider:
    """Deterministic in-memory provider for tests and offline runs."""

    def __init__(self, mapping: dict[str, list[EvidenceArticle] | None]) -> None:
        self._mapping = {key.lower(): value for key, value in mapping.items()}
        self.calls = 0

    @property
    def name(self) -> str:
        return "static"

    async def fetch_articles(
        self,
        subject: str,
        *,
        keywords: tuple[str, ...] = (),
        limit: int = 30,
        lookback_days: int = 7,
    ) -> list[EvidenceArticle] | None:
        del keywords, lookback_days
        self.calls += 1
        rows = self._mapping.get(subject.lower())
        return None if rows is None else list(rows[:limit])


__all__ = [
    "EvidenceProvider",
    "FallbackEvidenceProvider",
    "GDELTProvider",
    "GoogleNewsRSSProvider",
    "StaticEvidenceProvider",
]
