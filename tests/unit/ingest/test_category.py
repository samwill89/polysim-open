"""Category classifier tests."""

from __future__ import annotations

from polysim.config import CategoryEntry
from polysim.ingest.category import OTHER_CATEGORY, Classifier, KeywordClassifier


def _cats() -> dict[str, CategoryEntry]:
    return {
        "ai": CategoryEntry(enabled=True, tier="primary", keywords=["openai", "anthropic", "claude", "ai model"]),
        "aec": CategoryEntry(enabled=True, tier="primary", keywords=["autodesk", "procore"]),
        "creator": CategoryEntry(enabled=True, tier="primary", keywords=["mrbeast", "youtube"]),
        "gov_defense": CategoryEntry(enabled=False, tier="pilot", keywords=["lockheed", "raytheon"]),
    }


class TestKeywordClassifier:
    def test_direct_match(self) -> None:
        c = KeywordClassifier(_cats())
        assert c.classify("Will OpenAI release o4?") == "ai"
        assert c.classify("Autodesk revenue Q4?") == "aec"
        assert c.classify("MrBeast cameo?") == "creator"

    def test_disabled_category_ignored(self) -> None:
        c = KeywordClassifier(_cats())
        assert c.classify("Lockheed contract award?") is None

    def test_longest_keyword_wins(self) -> None:
        """'ai model' should beat 'ai' on tied questions (would never tie here
        since 'ai' isn't in keywords, but verifying the sort ordering)."""
        cats = {
            "ai": CategoryEntry(enabled=True, tier="primary", keywords=["ai", "ai model"]),
            "generic": CategoryEntry(enabled=True, tier="secondary", keywords=["a"]),
        }
        c = KeywordClassifier(cats)
        # Both "ai model" and "ai" in "Will AI model launch?" — longest wins
        assert c.classify("Will AI model launch?") == "ai"

    def test_case_insensitive(self) -> None:
        c = KeywordClassifier(_cats())
        assert c.classify("OPENAI announcement?") == "ai"

    def test_no_match(self) -> None:
        c = KeywordClassifier(_cats())
        assert c.classify("Will it rain tomorrow?") is None


class TestClassifier:
    async def test_falls_back_to_other(self) -> None:
        c = Classifier(_cats())
        assert await c.classify("Will it rain tomorrow?") == OTHER_CATEGORY

    async def test_keyword_hit(self) -> None:
        c = Classifier(_cats())
        assert await c.classify("OpenAI o4?") == "ai"

    async def test_llm_fallback_used_when_no_keyword(self) -> None:
        called_with: list[str] = []

        async def fake_llm(q: str) -> str | None:
            called_with.append(q)
            return "ai"

        c = Classifier(_cats(), llm_classify=fake_llm)
        assert await c.classify("Some unclear model question") == "ai"
        assert called_with == ["Some unclear model question"]

    async def test_llm_fallback_returning_unknown_category_becomes_other(self) -> None:
        async def fake_llm(_: str) -> str | None:
            return "totally_made_up"

        c = Classifier(_cats(), llm_classify=fake_llm)
        assert await c.classify("???") == OTHER_CATEGORY

    async def test_llm_fallback_exception_is_swallowed(self) -> None:
        async def fake_llm(_: str) -> str | None:
            raise RuntimeError("llm down")

        c = Classifier(_cats(), llm_classify=fake_llm)
        assert await c.classify("???") == OTHER_CATEGORY

    async def test_keyword_hit_skips_llm(self) -> None:
        called = False

        async def fake_llm(_: str) -> str | None:
            nonlocal called
            called = True
            return "ai"

        c = Classifier(_cats(), llm_classify=fake_llm)
        await c.classify("OpenAI release")
        assert not called
