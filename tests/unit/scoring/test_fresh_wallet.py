"""FreshWalletDetector tests."""

from __future__ import annotations

from polysim.scoring.fresh_wallet import FreshWalletDetector

from ._helpers import market, trade, wallet_profile


async def test_scores_fresh_wallet() -> None:
    det = FreshWalletDetector(fresh_nonce_threshold=10)
    p = wallet_profile(features={"nonce": 2})
    sig = await det.score(p, market(), trade())
    assert sig is not None
    assert 0 < sig.raw_score < 1


async def test_non_fresh_returns_none() -> None:
    det = FreshWalletDetector(fresh_nonce_threshold=10)
    p = wallet_profile(features={"nonce": 42})
    assert await det.score(p, market(), trade()) is None


async def test_missing_nonce_treated_as_zero() -> None:
    det = FreshWalletDetector(fresh_nonce_threshold=10)
    p = wallet_profile(features={})
    sig = await det.score(p, market(), trade())
    # nonce=0 → freshness=1.0
    assert sig is not None


async def test_larger_size_higher_score() -> None:
    det = FreshWalletDetector()
    p = wallet_profile(features={"nonce": 2})
    sig_small = await det.score(p, market(), trade(size_shares=10, price_cents=5))
    sig_big = await det.score(p, market(), trade(size_shares=10_000, price_cents=50))
    assert sig_small is not None and sig_big is not None
    assert sig_big.raw_score > sig_small.raw_score


async def test_trade_none_returns_none() -> None:
    det = FreshWalletDetector()
    p = wallet_profile(features={"nonce": 0})
    assert await det.score(p, market(), None) is None
