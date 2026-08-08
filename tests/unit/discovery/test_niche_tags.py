"""Niche tagging — empirical-priors addendum §3.6."""

from __future__ import annotations

import pytest

from polysim.discovery.niche_tags import (
    NICHE_TAGS_VERSION,
    primary_niches,
    tag_market,
)


def test_version_exists() -> None:
    assert isinstance(NICHE_TAGS_VERSION, str)
    assert NICHE_TAGS_VERSION.count(".") >= 2  # semver-shaped


def test_primary_niches_includes_three() -> None:
    n = primary_niches()
    for expected in ("aec", "ai_labs", "creator_econ"):
        assert expected in n


def test_aec_keyword_match() -> None:
    tags = tag_market(
        question="Will Autodesk beat Q1 revenue?",
        slug="autodesk-q1-2026",
    )
    assert "aec" in tags


def test_ai_labs_keyword_match() -> None:
    tags = tag_market(
        question="OpenAI ships GPT-6 by Q3?",
        slug="openai-gpt-6-q3",
    )
    assert "ai_labs" in tags


def test_creator_econ_match() -> None:
    tags = tag_market(
        question="MrBeast 100 Kids by May?",
        slug="mr-beast-100-kids-may",
    )
    assert "creator_econ" in tags


def test_multi_label_possible() -> None:
    tags = tag_market(
        question="Will Anthropic Claude become MrBeast's main AI tool?",
        slug=None,
    )
    # Both keywords hit — multi-label by design.
    assert "ai_labs" in tags
    assert "creator_econ" in tags


def test_no_match_returns_empty() -> None:
    tags = tag_market(
        question="Will the Dolphins win the Super Bowl?",
        slug="dolphins-sb-2027",
    )
    assert tags == []


@pytest.mark.parametrize("question", [None, "", "   "])
def test_empty_inputs_safe(question: str | None) -> None:
    tags = tag_market(question=question, slug=None)
    assert tags == []
