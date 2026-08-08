"""Pure extractors for intel-channel messages.

Given the raw text of a Telegram message, pull out:
  * wallet addresses (0x + 40 hex)
  * polymarket market slugs (URLs, bare slugs)
  * category keywords (matched against config.categories)

No network, no I/O, no side effects — unit-testable in isolation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Ethereum-style address, case-insensitive, word-bounded so we don't grab
# long hex strings mid-sentence or asset-id numbers.
_WALLET_RE = re.compile(r"\b(0[xX][0-9a-fA-F]{40})\b")

# Polymarket URLs we care about, in any of the flavours seen in the wild.
_URL_RE = re.compile(
    r"https?://(?:www\.)?polymarket\.com/"
    r"(?:market|event)/([a-z0-9][a-z0-9\-]{3,120})",
    re.IGNORECASE,
)

# A bare slug (no URL) — operators sometimes paste just the slug portion.
_BARE_SLUG_RE = re.compile(r"\b([a-z]{3,}(?:-[a-z0-9]+){2,})\b")

# Social-media / blog links that typically carry the actual intel detail.
# Channels like @spaceinsights reference wallets via these one-hop URLs
# rather than pasting the addresses inline.
_SOCIAL_LINK_RE = re.compile(
    r"https?://(?:www\.)?(?:"
    r"x\.com|twitter\.com|t\.me|warpcast\.com|dune\.com|polygonscan\.com|"
    r"arkhamintelligence\.com|arkm\.com|debank\.com|nansen\.ai|zerion\.io"
    r")/[^\s\]\)\}]+",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ExtractedIntel:
    wallets: list[str]
    market_slugs: list[str]
    categories: list[str]
    social_links: list[str]      # x.com / twitter.com / dune.com etc.


def extract_wallets(text: str) -> list[str]:
    """Return unique lowercased wallet addresses found in `text`."""
    seen: set[str] = set()
    out: list[str] = []
    for m in _WALLET_RE.findall(text or ""):
        a = m.lower()
        if a not in seen:
            seen.add(a)
            out.append(a)
    return out


def extract_market_slugs(text: str) -> list[str]:
    """Pull polymarket.com slugs from URLs + likely bare slugs.

    Bare slugs are only kept when they look specific enough (≥ 3 segments)
    to avoid grabbing generic phrases.
    """
    seen: set[str] = set()
    out: list[str] = []
    for match in _URL_RE.findall(text or ""):
        s = match.lower()
        if s not in seen:
            seen.add(s)
            out.append(s)
    # Strip URL-matched slugs before looking for bare ones, so we don't
    # double-count a slug that appeared inside a URL.
    stripped = _URL_RE.sub(" ", text or "")
    for m in _BARE_SLUG_RE.findall(stripped):
        s = m.lower()
        # must have at least 2 hyphens to look slug-ish
        if s.count("-") < 2:
            continue
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def extract_categories(
    text: str, *, keywords_by_category: dict[str, list[str]]
) -> list[str]:
    """Return categories whose keyword list has a case-insensitive hit."""
    if not text:
        return []
    lower = text.lower()
    hits: list[str] = []
    for cat, kws in keywords_by_category.items():
        for kw in kws:
            if not kw:
                continue
            if kw.lower() in lower:
                hits.append(cat)
                break
    return hits


def extract_social_links(text: str) -> list[str]:
    """Return unique external links pointing at likely intel sources."""
    seen: set[str] = set()
    out: list[str] = []
    for m in _SOCIAL_LINK_RE.finditer(text or ""):
        url = m.group(0).rstrip(".,;:)")
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


def extract_all(
    text: str, *, keywords_by_category: dict[str, list[str]] | None = None
) -> ExtractedIntel:
    return ExtractedIntel(
        wallets=extract_wallets(text),
        market_slugs=extract_market_slugs(text),
        categories=(
            extract_categories(text, keywords_by_category=keywords_by_category)
            if keywords_by_category else []
        ),
        social_links=extract_social_links(text),
    )


__all__ = [
    "ExtractedIntel",
    "extract_all",
    "extract_categories",
    "extract_market_slugs",
    "extract_wallets",
]
