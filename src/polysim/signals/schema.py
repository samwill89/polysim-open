"""Shared types for the external conversation-signal layer.

The signals package turns *public conversation activity* (Reddit today;
any forum tomorrow) into a per-market conviction score the trading side
can consume. The contract is deliberately narrow:

  ConversationPost   one normalized public post (provider output)
  TopicActivity      one topic's windowed activity metrics (persisted)
  MarketSignal       one market's scored signal at a point in time
                     (persisted; consumed by sizing / gates / reports)

Direction policy (v1, deterministic): the *cohort wallet* supplies trade
direction; conversation supplies **conviction only** (attention, velocity,
engagement, breadth). `stance` is extracted and stored for the LLM
investigator prompt + later evaluation, but it does NOT sign the
composite — inferring "positive chatter ⇒ YES" without reading the market
question is exactly the kind of silent wrongness we refuse to encode.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ConversationPost(_Frozen):
    """One normalized public post from any provider."""

    source: str                        # 'reddit'
    topic: str                         # normalized community key, e.g. 'boxoffice'
    external_id: str
    created_at: datetime
    title: str
    text: str = ""
    author: str | None = None
    score: int = 0                     # provider upvote-ish metric
    num_comments: int = 0
    url: str | None = None


class TopicActivity(_Frozen):
    """Windowed activity metrics for one (source, topic)."""

    source: str
    topic: str
    window_start: datetime
    window_end: datetime
    n_posts: int = Field(ge=0)
    n_comments: int = Field(ge=0)
    score_sum: int = 0
    unique_authors: int = Field(ge=0)

    @property
    def window_hours(self) -> float:
        secs = (self.window_end - self.window_start).total_seconds()
        return max(secs / 3600.0, 1e-9)

    @property
    def posts_per_hour(self) -> float:
        return self.n_posts / self.window_hours


class MarketSignal(_Frozen):
    """Scored conversation signal for one market.

    `composite` ∈ [0, 1] is *unsigned conviction*: how alive/expanding the
    public conversation around this market is. 0.5 ≈ typical baseline;
    >0.5 attention is elevated; <0.5 the conversation is dead(ening).
    `confidence` ∈ [0, 1] reflects data sufficiency (matched posts +
    baseline history), NOT correctness. `stance` ∈ [-1, 1] is recorded
    for prompts/evaluation only.
    """

    market_id: str
    ts: datetime
    category: str | None = None
    provider: str = "reddit"
    topics: list[str] = Field(default_factory=list)
    matched_terms: list[str] = Field(default_factory=list)
    n_posts: int = Field(default=0, ge=0)
    n_matched_posts: int = Field(default=0, ge=0)
    attention_z: float = 0.0
    velocity: float = 1.0              # current vs trailing posts/hr ratio
    engagement: float = 0.0            # comments per post, normalized [0,1]
    breadth: float = 0.0               # unique authors / posts, [0,1]
    stance: float = Field(default=0.0, ge=-1.0, le=1.0)
    composite: float = Field(default=0.5, ge=0.0, le=1.0)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    components: dict[str, Any] = Field(default_factory=dict)


__all__ = ["ConversationPost", "MarketSignal", "TopicActivity"]
