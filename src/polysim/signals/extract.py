"""Pure feature extraction over conversation posts.

Everything here is deterministic given (posts, now) — no I/O, no LLM.
That keeps the signal reproducible from stored inputs (spec §14 #5) and
trivially testable against fixtures.
"""

from __future__ import annotations

import math
import re
import statistics
from collections.abc import Sequence
from datetime import datetime, timedelta

from polysim.signals.schema import ConversationPost, TopicActivity

# ── market-question term extraction ──────────────────────

# Words that carry no entity information in market questions.
_STOPWORDS = frozenset({
    "will", "the", "a", "an", "of", "in", "on", "by", "at", "to", "for",
    "and", "or", "be", "is", "are", "was", "were", "before", "after",
    "than", "more", "less", "over", "under", "above", "below", "between",
    "this", "that", "its", "his", "her", "their", "with", "without",
    "have", "has", "had", "do", "does", "did", "not", "no", "yes",
    "what", "when", "which", "who", "how", "why", "any", "all",
    "million", "billion", "weekend", "opening", "domestic", "worldwide",
    "gross", "box", "office", "market", "resolve", "resolves", "reach",
    "hit", "win", "make", "release", "released", "day", "week", "month",
    "year", "end", "first", "new", "top", "next",
})

_QUOTE_CHARS = "\"'\\u2018\\u2019\\u201c\\u201d"
_QUOTED_RE = re.compile(f"[{_QUOTE_CHARS}]([^{_QUOTE_CHARS}]{{2,60}})[{_QUOTE_CHARS}]")
# Runs of Capitalized words (allowing digits, ampersands, hyphens, colons).
_CAPSEQ_RE = re.compile(r"\b([A-Z][\w&'-]*(?::?\s+[A-Z0-9][\w&'-]*){0,5})\b")
_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9'-]{3,}")


def extract_market_terms(question: str, *, max_terms: int = 8) -> list[str]:
    """Distinctive lowercase search terms from a market question.

    Order of preference: quoted spans, capitalized sequences (entity-ish),
    then distinctive standalone tokens. All lowercased and deduped.
    """
    terms: list[str] = []
    seen: set[str] = set()

    def _add(raw: str) -> None:
        t = " ".join(raw.strip().lower().split())
        if len(t) < 3 or t in seen or t in _STOPWORDS:
            return
        seen.add(t)
        terms.append(t)

    for m in _QUOTED_RE.finditer(question):
        _add(m.group(1))

    # Skip the leading word of the question ("Will ...") for cap-sequences.
    body = question.split(" ", 1)[1] if " " in question else question
    for m in _CAPSEQ_RE.finditer(body):
        seq = m.group(1)
        words = [w for w in re.split(r"\s+", seq) if w]
        content = [w for w in words if w.lower() not in _STOPWORDS]
        if not content:
            continue
        _add(" ".join(content))

    for tok in _TOKEN_RE.findall(question.lower()):
        if tok not in _STOPWORDS:
            _add(tok)

    return terms[:max_terms]


def match_posts(
    posts: Sequence[ConversationPost], terms: Sequence[str]
) -> list[ConversationPost]:
    """Posts whose title/text mention any of the (lowercase) terms."""
    if not terms:
        return []
    out: list[ConversationPost] = []
    for p in posts:
        blob = f"{p.title}\n{p.text}".lower()
        if any(t in blob for t in terms):
            out.append(p)
    return out


# ── windowed activity ────────────────────────────────────


def activity_from_posts(
    posts: Sequence[ConversationPost],
    *,
    source: str,
    topic: str,
    now: datetime,
    window_hours: float,
) -> TopicActivity:
    """Aggregate posts inside [now - window, now] into a TopicActivity."""
    start = now - timedelta(hours=window_hours)
    inside = [p for p in posts if start <= p.created_at <= now]
    authors = {p.author for p in inside if p.author}
    return TopicActivity(
        source=source,
        topic=topic,
        window_start=start,
        window_end=now,
        n_posts=len(inside),
        n_comments=sum(max(0, p.num_comments) for p in inside),
        score_sum=sum(p.score for p in inside),
        unique_authors=len(authors),
    )


def attention_zscore(current: float, history: Sequence[float]) -> float:
    """Z-score of `current` against trailing history (same convention as
    the equity lane's `_zscore`: needs >= 5 points, else 0.0)."""
    if len(history) < 5:
        return 0.0
    mu = statistics.mean(history)
    sd = statistics.pstdev(history)
    if sd <= 0:
        return 0.0
    return (current - mu) / sd


def velocity_ratio(current_posts_per_hour: float, history_pph: Sequence[float]) -> float:
    """Current posting rate vs trailing mean rate. 1.0 = unchanged.

    With no usable history the ratio is 1.0 (neutral), never inf/NaN.
    """
    usable = [v for v in history_pph if v > 0]
    if not usable:
        return 1.0
    base = statistics.mean(usable)
    if base <= 0:
        return 1.0
    return max(0.0, current_posts_per_hour) / base


def engagement_norm(n_posts: int, n_comments: int, *, midpoint: float = 5.0) -> float:
    """Comments-per-post squashed to [0, 1); `midpoint` c/p → 0.5."""
    if n_posts <= 0:
        return 0.0
    cpp = n_comments / n_posts
    return cpp / (cpp + midpoint)


def breadth_norm(n_posts: int, unique_authors: int) -> float:
    """Unique authors / posts ∈ [0, 1]. 1.0 = every post a distinct voice."""
    if n_posts <= 0:
        return 0.0
    return min(1.0, unique_authors / n_posts)


# ── stance (recorded only — never signs the composite) ───

_POSITIVE = frozenset({
    "up", "surge", "beat", "beats", "record", "strong", "huge", "hype",
    "hit", "smash", "soar", "soars", "rally", "bullish", "confirmed",
    "success", "win", "wins", "won", "great", "amazing", "tracking up",
    "exceed", "exceeds", "outperform", "sold out", "breakout",
})
_NEGATIVE = frozenset({
    "down", "flop", "flops", "miss", "misses", "weak", "bomb", "bombs",
    "drop", "drops", "crash", "bearish", "delay", "delayed", "cancel",
    "cancelled", "canceled", "fail", "fails", "failed", "bad", "terrible",
    "underperform", "disappointing", "tanks", "tanked", "cut", "layoffs",
})
_WORD_RE = re.compile(r"[a-z][a-z'-]*")


def stance_of_posts(posts: Sequence[ConversationPost]) -> float:
    """Crude lexicon polarity ∈ [-1, 1] over matched posts.

    Deliberately weak — stored for the investigator prompt and for later
    calibration work, with **zero weight** in the composite (see
    scoring.compose_market_signal).
    """
    pos = neg = 0
    for p in posts:
        words = set(_WORD_RE.findall(f"{p.title}\n{p.text}".lower()))
        pos += len(words & _POSITIVE)
        neg += len(words & _NEGATIVE)
    total = pos + neg
    if total == 0:
        return 0.0
    return (pos - neg) / total


def sigmoid(x: float) -> float:
    """Numerically safe logistic squash → (0, 1)."""
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


__all__ = [
    "activity_from_posts",
    "attention_zscore",
    "breadth_norm",
    "engagement_norm",
    "extract_market_terms",
    "match_posts",
    "sigmoid",
    "stance_of_posts",
    "velocity_ratio",
]
