"""Pure feature-extraction tests — deterministic given (posts, now)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from polysim.signals.extract import (
    activity_from_posts,
    attention_zscore,
    breadth_norm,
    engagement_norm,
    extract_market_terms,
    match_posts,
    sigmoid,
    stance_of_posts,
    velocity_ratio,
)
from polysim.signals.schema import ConversationPost

NOW = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


def _post(
    ext_id: str,
    *,
    hours_ago: float = 1.0,
    title: str = "t",
    text: str = "",
    author: str | None = "u1",
    num_comments: int = 0,
    score: int = 0,
) -> ConversationPost:
    return ConversationPost(
        source="reddit", topic="boxoffice", external_id=ext_id,
        created_at=NOW - timedelta(hours=hours_ago),
        title=title, text=text, author=author,
        num_comments=num_comments, score=score,
    )


# ── term extraction ──────────────────────────────────────


def test_terms_pick_quoted_titles() -> None:
    terms = extract_market_terms('Will "Dune: Part Three" gross $100M opening weekend?')
    assert "dune: part three" in terms


def test_terms_pick_capitalized_entities() -> None:
    terms = extract_market_terms("Will Superman Legacy open above $125M domestic?")
    assert any("superman" in t for t in terms)


def test_terms_skip_leading_will_and_stopwords() -> None:
    terms = extract_market_terms("Will the opening weekend gross exceed $90M?")
    assert "will" not in terms
    assert "the" not in terms


def test_terms_deterministic_and_bounded() -> None:
    q = 'Will "Avatar 4" beat Avengers Doomsday at the worldwide box office in 2026?'
    a = extract_market_terms(q)
    b = extract_market_terms(q)
    assert a == b
    assert len(a) <= 8


# ── matching ─────────────────────────────────────────────


def test_match_posts_finds_term_in_title_or_text() -> None:
    posts = [
        _post("1", title="Dune: Part Three tracking way up"),
        _post("2", title="Weekend thread", text="dune: part three looks huge"),
        _post("3", title="Unrelated marvel news"),
    ]
    matched = match_posts(posts, ["dune: part three"])
    assert {p.external_id for p in matched} == {"1", "2"}


def test_match_posts_empty_terms_matches_nothing() -> None:
    assert match_posts([_post("1")], []) == []


# ── windowed activity ────────────────────────────────────


def test_activity_counts_only_inside_window() -> None:
    posts = [
        _post("1", hours_ago=1, num_comments=4, author="a"),
        _post("2", hours_ago=23, num_comments=6, author="b"),
        _post("3", hours_ago=30, num_comments=100, author="c"),  # outside
    ]
    act = activity_from_posts(
        posts, source="reddit", topic="boxoffice", now=NOW, window_hours=24,
    )
    assert act.n_posts == 2
    assert act.n_comments == 10
    assert act.unique_authors == 2
    assert act.posts_per_hour == pytest.approx(2 / 24)


# ── statistics helpers ───────────────────────────────────


def test_zscore_needs_five_points() -> None:
    assert attention_zscore(50, [10, 10, 10, 10]) == 0.0


def test_zscore_flags_spike() -> None:
    z = attention_zscore(20, [10, 10, 10, 10, 10, 12, 8])
    assert z > 3.0


def test_zscore_zero_variance_is_zero() -> None:
    assert attention_zscore(10, [10, 10, 10, 10, 10]) == 0.0


def test_velocity_neutral_without_history() -> None:
    assert velocity_ratio(5.0, []) == 1.0
    assert velocity_ratio(5.0, [0.0, 0.0]) == 1.0


def test_velocity_ratio_doubles() -> None:
    assert velocity_ratio(4.0, [2.0, 2.0, 2.0]) == pytest.approx(2.0)


def test_engagement_midpoint() -> None:
    # 5 comments/post with midpoint 5 → 0.5
    assert engagement_norm(2, 10) == pytest.approx(0.5)
    assert engagement_norm(0, 100) == 0.0


def test_breadth_bounds() -> None:
    assert breadth_norm(4, 4) == 1.0
    assert breadth_norm(4, 2) == 0.5
    assert breadth_norm(0, 0) == 0.0


def test_sigmoid_symmetry() -> None:
    assert sigmoid(0.0) == pytest.approx(0.5)
    assert sigmoid(3.0) + sigmoid(-3.0) == pytest.approx(1.0)


# ── stance (recorded-only) ───────────────────────────────


def test_stance_positive_negative_neutral() -> None:
    up = [_post("1", title="Huge weekend, record breaking, tracking up")]
    down = [_post("2", title="Total flop, disappointing drop")]
    flat = [_post("3", title="Weekend numbers thread")]
    assert stance_of_posts(up) > 0
    assert stance_of_posts(down) < 0
    assert stance_of_posts(flat) == 0.0
    assert -1.0 <= stance_of_posts(up + down) <= 1.0
