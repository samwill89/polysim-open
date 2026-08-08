"""EventInsiderDetector tests."""

from __future__ import annotations

from polysim.scoring.event_insider import EventInsiderDetector

from ._helpers import market, trade, wallet_profile


def _ip(nonce: int = 3, mid_cents: int = 22) -> dict[str, object]:
    return {"nonce": nonce, "market_mid_cents": mid_cents}


async def test_all_gates_pass_fires() -> None:
    det = EventInsiderDetector()
    # mid=22, price=50 → contrarian 28pts = 2800bps > 1500
    p = wallet_profile(features=_ip(nonce=3, mid_cents=22))
    m = market(volume_cents=47_200_00)
    # 20000 * 50 = 1,000,000 cents → above 500k size gate
    sig = await det.score(p, m, trade(price_cents=50, size_shares=20000))
    assert sig is not None
    assert sig.raw_score > 0
    assert set(sig.components) >= {
        "nonce_feature", "size_feature", "volume_feature", "contrarian_feature"
    }


async def test_non_fresh_wallet_blocks() -> None:
    det = EventInsiderDetector(fresh_nonce_threshold=10)
    p = wallet_profile(features=_ip(nonce=50))
    assert await det.score(p, market(), trade()) is None


async def test_too_small_blocks() -> None:
    det = EventInsiderDetector(fresh_size_min_cents=500_000)
    p = wallet_profile(features=_ip(nonce=3))
    # 10 shares * 32 cents = 320 cents ($3.20) — well below threshold
    assert await det.score(p, market(), trade(size_shares=10)) is None


async def test_high_volume_market_blocks() -> None:
    det = EventInsiderDetector(niche_market_vol_max_cents=50_000_000)
    p = wallet_profile(features=_ip(nonce=3))
    assert await det.score(p, market(volume_cents=99_999_999_99), trade()) is None


async def test_non_contrarian_blocks() -> None:
    det = EventInsiderDetector(contrarian_bps=1500)
    # Trade at 32¢ with mid at 31¢ → only 1pt = 100bps, under 1500
    p = wallet_profile(features=_ip(nonce=3, mid_cents=31))
    assert await det.score(p, market(), trade(price_cents=32)) is None


async def test_monotonic_in_nonce() -> None:
    det = EventInsiderDetector()
    m = market()
    # Contrarian 28pts = 2800bps > 1500; size 1M cents > 500k.
    t = trade(size_shares=20000, price_cents=50)
    scores = []
    for n in (0, 1, 3, 6, 9):
        p = wallet_profile(features=_ip(nonce=n, mid_cents=22))
        s = await det.score(p, m, t)
        assert s is not None, f"expected signal for nonce={n}"
        scores.append(s.raw_score)
    # Lower nonce → higher (or equal) score
    from itertools import pairwise
    for earlier, later in pairwise(scores):
        assert earlier >= later - 1e-9


async def test_monotonic_in_contrarian() -> None:
    det = EventInsiderDetector(contrarian_bps=1500)
    m = market()
    p = wallet_profile(features=_ip(nonce=3, mid_cents=22))
    scores = []
    for price in (37, 45, 60, 80):  # increasing contrarian
        # size_shares chosen so size_cents > 500,000 for all prices.
        s = await det.score(p, m, trade(price_cents=price, size_shares=20000))
        assert s is not None, f"expected signal at price={price}"
        scores.append(s.raw_score)
    from itertools import pairwise
    for earlier, later in pairwise(scores):
        assert later >= earlier - 1e-9
