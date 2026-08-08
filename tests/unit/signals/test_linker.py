"""Market → topic linking, including intel_sources fallback derivation."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from polysim.config import Config
from polysim.models import Market
from polysim.signals.linker import category_topics_from_config, link_market


def _cfg(**signals: Any) -> Config:
    return Config.model_validate({
        "run": {"name": "t", "mode": "live_paper"},
        "categories": {},
        "intel_sources": {
            "box_office": {"reddit": ["boxoffice", "r/TrueFilm"]},
            "ai": {"reddit": ["LocalLLaMA", "singularity"]},
            "sports": {"reddit": []},
        },
        "signals": signals,
    })


def _market(question: str, category: str | None) -> Market:
    return Market(
        id="m1", slug="s", question=question,
        category=category,  # type: ignore[arg-type]
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def test_topics_derive_from_intel_sources_when_unset() -> None:
    topics = category_topics_from_config(_cfg())
    assert topics["box_office"] == ["boxoffice", "truefilm"]
    assert topics["ai"] == ["localllama", "singularity"]
    assert "sports" not in topics  # empty list drops out


def test_explicit_category_map_wins() -> None:
    topics = category_topics_from_config(
        _cfg(category_subreddits={"box_office": ["r/movies"]}),
    )
    assert topics == {"box_office": ["movies"]}


def test_topics_capped_per_category() -> None:
    topics = category_topics_from_config(
        _cfg(category_subreddits={"ai": ["a", "b", "c", "d", "e"]}),
    )
    assert len(topics["ai"]) == 3  # default max_subreddits_per_category


def test_link_market_box_office() -> None:
    cat_topics = category_topics_from_config(_cfg())
    link = link_market(
        _market('Will "Dune: Part Three" gross $100M opening weekend?', "box_office"),
        cat_topics,
    )
    assert link.linked
    assert "boxoffice" in link.topics
    assert "dune: part three" in link.terms


def test_link_market_unmapped_category_is_unlinked() -> None:
    link = link_market(_market("Will X happen?", "other"), {})
    assert not link.linked
    assert link.topics == ()
