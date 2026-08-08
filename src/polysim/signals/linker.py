"""Market → conversation-topic linking.

A market gets (a) the subreddits configured for its category and (b) a
set of distinctive search terms pulled from its question. Both are
deterministic. The category→subreddit map comes from
`config.signals.category_subreddits` when set, else it is derived from
the existing `intel_sources` niches — box_office already lists
r/boxoffice there, so movie markets link with zero extra config.
"""

from __future__ import annotations

from dataclasses import dataclass

from polysim.config import Config
from polysim.models import Market
from polysim.signals.extract import extract_market_terms
from polysim.signals.providers import normalize_topic


@dataclass(frozen=True)
class MarketTopicLink:
    """One market's linkage into conversation space."""

    market_id: str
    category: str | None
    topics: tuple[str, ...]           # normalized subreddit names
    terms: tuple[str, ...]            # lowercase search terms

    @property
    def linked(self) -> bool:
        return bool(self.topics)


def category_topics_from_config(
    cfg: Config, *, max_per_category: int | None = None
) -> dict[str, list[str]]:
    """category → [subreddit, ...], normalized and bounded.

    Explicit `signals.category_subreddits` wins; otherwise fall back to
    the `intel_sources` reddit lists (same niche keys as categories).
    """
    explicit = cfg.signals.category_subreddits
    raw: dict[str, list[str]] = (
        {k: list(v) for k, v in explicit.items()}
        if explicit
        else {k: list(v.reddit) for k, v in cfg.intel_sources.items()}
    )
    cap = max_per_category or cfg.signals.max_subreddits_per_category
    out: dict[str, list[str]] = {}
    for cat, subs in raw.items():
        normed: list[str] = []
        for s in subs:
            n = normalize_topic(s)
            if n and n not in normed:
                normed.append(n)
        if normed:
            out[cat] = normed[:cap]
    return out


def link_market(
    market: Market, category_topics: dict[str, list[str]]
) -> MarketTopicLink:
    """Resolve one market to its topics + search terms."""
    cat = str(market.category) if market.category else None
    topics = tuple(category_topics.get(cat, [])) if cat else ()
    terms = tuple(extract_market_terms(market.question))
    return MarketTopicLink(
        market_id=market.id,
        category=cat,
        topics=topics,
        terms=terms,
    )


__all__ = ["MarketTopicLink", "category_topics_from_config", "link_market"]
