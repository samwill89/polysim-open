"""Niche tagging — empirical-priors addendum §3.6.

Multi-label market tagger. Each PolySim target niche has a keyword list +
slug-pattern list; a market gets every tag whose criteria hit. Tags drive
cohort selection (§3.5) and weighting in the consensus layer (§5.4).

`NICHE_TAGS_VERSION` is bumped any time the dict below changes. The
experiment record stamps the version at cohort-freeze time; mismatch
between cohort version and current version blocks the trading loop
to prevent silent cohort drift.
"""

from __future__ import annotations

import re
from typing import Final

# v1.0.0 — initial tag set, addendum §3.6.
NICHE_TAGS_VERSION: Final[str] = "1.0.0"


# Keyword + slug pattern lists per niche. Matching is case-insensitive.
NICHE_TAGS: Final[dict[str, dict[str, list[str]]]] = {
    "aec": {
        "keywords": [
            "autodesk", "procore", "bentley", "trimble",
            "bim", "construction", "architecture", "engineering firm",
            "cad software", "revit", "autocad",
            "construction software", "aec",
            "construction tech", "permits issued",
        ],
        "slug_patterns": [
            r"autodesk-.*", r"procore-.*", r"bentley-.*",
            r"trimble-.*", r".*-construction-.*", r".*-bim-.*",
        ],
    },
    "ai_labs": {
        "keywords": [
            "openai", "anthropic", "deepmind", "xai",
            "google deepmind", "mistral",
            "gpt-", "claude", "gemini", "grok",
            "llama", "model release", "ai lab",
            "frontier model", "agi", "sora", "operator",
        ],
        "slug_patterns": [
            r".*-gpt-\d+.*", r"claude-.*", r"gemini-.*",
            r"openai-.*", r"anthropic-.*", r".*-model-release-.*",
        ],
    },
    "creator_econ": {
        "keywords": [
            "youtube", "tiktok", "substack", "twitch",
            "creator", "patreon", "streamer", "subscriber",
            "subs hit", "subscriber count", "channel",
            "mr beast", "mrbeast", "kai cenat", "speed",
            "podcast", "joe rogan",
        ],
        "slug_patterns": [
            r"mr-beast-.*", r"mrbeast-.*",
            r".*-youtube-subs-.*", r".*-tiktok-.*",
            r"kai-cenat-.*", r"joe-rogan-.*",
        ],
    },
}


# Pre-compile slug patterns once.
_COMPILED_PATTERNS: dict[str, list[re.Pattern[str]]] = {
    niche: [re.compile(p, re.IGNORECASE) for p in spec["slug_patterns"]]
    for niche, spec in NICHE_TAGS.items()
}


def tag_market(
    *, question: str | None, slug: str | None
) -> list[str]:
    """Return every niche tag that matches `question` or `slug`.

    Multi-label by design — a market can be both `ai_labs` and `general`.
    Returns an empty list when no niche matches.
    """
    tags: list[str] = []
    q_lower = (question or "").lower()
    s_lower = (slug or "").lower()
    for niche, spec in NICHE_TAGS.items():
        # Keyword match against question text.
        kw_hit = any(kw.lower() in q_lower for kw in spec["keywords"])
        # Slug-pattern match.
        slug_hit = any(p.search(s_lower) for p in _COMPILED_PATTERNS[niche])
        if kw_hit or slug_hit:
            tags.append(niche)
    return tags


def primary_niches() -> list[str]:
    """The three target niches that drive the primary cohort pool (§3.5)."""
    return list(NICHE_TAGS.keys())


__all__ = [
    "NICHE_TAGS",
    "NICHE_TAGS_VERSION",
    "primary_niches",
    "tag_market",
]
