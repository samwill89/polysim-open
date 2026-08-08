"""CategoryInsiderDetector tests."""

from __future__ import annotations

import pytest

from polysim.scoring.category_insider import CategoryInsiderDetector

from ._helpers import market, trade, wallet_profile


async def test_score_signal_for_high_winrate_wallet() -> None:
    det = CategoryInsiderDetector(min_resolved_markets=8)
    p = wallet_profile(wins=10, losses=0, categories={"ai": 10})
    m = market()
    sig = await det.score(p, m, trade())
    assert sig is not None
    assert sig.raw_score > 0.9
    assert sig.confidence > 0
    assert "p_value" in sig.components


async def test_insufficient_history_returns_none() -> None:
    det = CategoryInsiderDetector(min_resolved_markets=8)
    p = wallet_profile(wins=3, losses=1, categories={"ai": 4})
    assert await det.score(p, market(), trade()) is None


async def test_no_category_returns_none() -> None:
    det = CategoryInsiderDetector()
    p = wallet_profile(categories={"ai": 10})
    assert await det.score(p, market(category=None), trade()) is None


async def test_perfectly_winning_wallet_capped() -> None:
    det = CategoryInsiderDetector(min_resolved_markets=8)
    p = wallet_profile(wins=20, losses=0, categories={"ai": 20})
    sig = await det.score(p, market(), trade())
    assert sig is not None
    assert sig.raw_score <= 0.999


async def test_losing_wallet_low_score() -> None:
    det = CategoryInsiderDetector(min_resolved_markets=8)
    p = wallet_profile(wins=2, losses=8, categories={"ai": 10})
    sig = await det.score(p, market(), trade())
    assert sig is not None
    assert sig.raw_score < 0.5


async def test_wallet_in_wrong_category_returns_none() -> None:
    det = CategoryInsiderDetector(min_resolved_markets=8)
    p = wallet_profile(categories={"aec": 10})
    assert await det.score(p, market(category="ai"), trade()) is None


def test_invalid_base_rate_raises() -> None:
    with pytest.raises(ValueError):
        CategoryInsiderDetector(base_rate=0.0)
    with pytest.raises(ValueError):
        CategoryInsiderDetector(base_rate=1.0)
