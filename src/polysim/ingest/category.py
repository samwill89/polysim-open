"""Category classifier — keyword match first, Haiku LLM fallback.

Build plan §1.4. Classification is deterministic when a keyword matches
(important for spec §14 #5 reproducibility). The Haiku fallback is opt-in
via config and caches its verdicts in the `meta` table.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping

from polysim.config import CategoryEntry

LLMClassifier = Callable[[str], Awaitable[str | None]]

log = logging.getLogger(__name__)

OTHER_CATEGORY = "other"


class KeywordClassifier:
    """Pure keyword-match classifier built from config.categories."""

    def __init__(self, categories: Mapping[str, CategoryEntry]) -> None:
        # Keep only enabled categories. Precompute lower-cased keywords
        # with their source category for O(total-keywords) classify().
        pairs: list[tuple[str, str]] = []  # (keyword_lower, category)
        for cat_name, entry in categories.items():
            if not entry.enabled:
                continue
            for kw in entry.keywords:
                kw_l = kw.strip().lower()
                if kw_l:
                    pairs.append((kw_l, cat_name))
        # Sort longest keyword first so "ai model" beats "ai" on tie.
        pairs.sort(key=lambda p: len(p[0]), reverse=True)
        self._keywords = pairs

    def classify(self, question: str) -> str | None:
        q = question.lower()
        for kw, cat in self._keywords:
            if kw in q:
                return cat
        return None

    def is_known_category(self, name: str) -> bool:
        return any(cat == name for _, cat in self._keywords)


class Classifier:
    """Keyword-first classifier with an (optional) LLM fallback.

    Phase 1 ships with the LLM fallback path wired but disabled by default.
    Enable it in Phase 2+ by passing a non-None `llm_classify` callable.
    """

    def __init__(
        self,
        categories: Mapping[str, CategoryEntry],
        *,
        llm_classify: LLMClassifier | None = None,
    ) -> None:
        self._keyword = KeywordClassifier(categories)
        self._llm_classify = llm_classify

    async def classify(self, question: str) -> str:
        """Return one of the configured categories, or OTHER_CATEGORY."""
        cat = self._keyword.classify(question)
        if cat is not None:
            return cat
        if self._llm_classify is None:
            return OTHER_CATEGORY
        # Phase 2+: LLM fallback
        try:
            result = await self._llm_classify(question)
        except Exception as exc:
            log.warning("llm category fallback failed: %s", exc)
            return OTHER_CATEGORY
        if isinstance(result, str) and self._keyword.is_known_category(result):
            return result
        return OTHER_CATEGORY
