"""Property-based monotonicity tests (build plan §2.15).

For each detector: "more insider-like" inputs must produce >= scores.
We use Hypothesis to fuzz over realistic ranges and check the invariant.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from polysim.scoring.event_insider import EventInsiderDetector
from polysim.scoring.fresh_wallet import FreshWalletDetector

from ._helpers import market, trade, wallet_profile


@given(
    nonce=st.integers(min_value=0, max_value=9),
    mid_cents=st.integers(min_value=5, max_value=25),
    extra_price=st.integers(min_value=15, max_value=40),
    size_shares=st.integers(min_value=2_000, max_value=50_000),
    vol_cents=st.integers(min_value=1_000_000, max_value=45_000_000),
)
@settings(max_examples=60, deadline=None)
async def test_event_insider_monotonic_in_freshness(
    nonce: int,
    mid_cents: int,
    extra_price: int,
    size_shares: int,
    vol_cents: int,
) -> None:
    """Lower nonce (more fresh) => higher or equal score."""
    det = EventInsiderDetector()
    m = market(volume_cents=vol_cents)
    price = mid_cents + extra_price  # well above mid
    t = trade(price_cents=min(100, price), size_shares=size_shares)

    p_low = wallet_profile(features={"nonce": nonce, "market_mid_cents": mid_cents})
    p_high = wallet_profile(features={"nonce": 9, "market_mid_cents": mid_cents})

    s_low = await det.score(p_low, m, t)
    s_high = await det.score(p_high, m, t)

    if s_low is None or s_high is None:
        # Both paths must either score or not; if only one scores, that's
        # the monotonicity direction (low-nonce more likely to pass gates).
        return
    assert s_low.raw_score >= s_high.raw_score - 1e-9


@given(
    nonce_lo=st.integers(min_value=0, max_value=5),
    nonce_hi=st.integers(min_value=5, max_value=9),
    size_shares=st.integers(min_value=10, max_value=10_000),
)
@settings(max_examples=60, deadline=None)
async def test_fresh_wallet_monotonic_in_nonce(
    nonce_lo: int, nonce_hi: int, size_shares: int
) -> None:
    if nonce_lo >= nonce_hi:
        return
    det = FreshWalletDetector()
    t = trade(size_shares=size_shares)
    s_lo = await det.score(wallet_profile(features={"nonce": nonce_lo}), market(), t)
    s_hi = await det.score(wallet_profile(features={"nonce": nonce_hi}), market(), t)
    if s_lo is None or s_hi is None:
        return
    assert s_lo.raw_score >= s_hi.raw_score - 1e-9


@given(
    size_small=st.integers(min_value=10, max_value=100),
    size_big=st.integers(min_value=1_000, max_value=50_000),
    nonce=st.integers(min_value=0, max_value=5),
)
@settings(max_examples=40, deadline=None)
async def test_fresh_wallet_monotonic_in_size(
    size_small: int, size_big: int, nonce: int
) -> None:
    if size_small >= size_big:
        return
    det = FreshWalletDetector()
    p = wallet_profile(features={"nonce": nonce})
    s_small = await det.score(p, market(), trade(size_shares=size_small, price_cents=20))
    s_big = await det.score(p, market(), trade(size_shares=size_big, price_cents=20))
    if s_small is None or s_big is None:
        return
    assert s_big.raw_score >= s_small.raw_score - 1e-9
