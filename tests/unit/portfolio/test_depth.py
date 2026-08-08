"""Order-book depth-aware sizing — empirical-priors addendum §4.4."""

from __future__ import annotations

import pytest

from polysim.portfolio.depth import (
    DEFAULT_DEGEN_DEPTH_PCT,
    DEFAULT_SYSTEMATIC_DEPTH_PCT,
    cap_size_to_depth,
    check_depth,
    top_n_size_shares,
)


def _book(*sizes: int) -> list[dict[str, int]]:
    return [{"price_cents": 50, "size_shares": s} for s in sizes]


def test_top_n_sums_first_n_levels() -> None:
    book = _book(100, 200, 300, 400)
    assert top_n_size_shares(book, n=3) == 600
    assert top_n_size_shares(book, n=1) == 100
    assert top_n_size_shares(book, n=10) == 1000


def test_top_n_robust_to_malformed() -> None:
    bad = [{"size_shares": 100}, "junk", {"price_cents": 40}, {"size_shares": "x"}]
    assert top_n_size_shares(bad) == 100
    assert top_n_size_shares([]) == 0
    assert top_n_size_shares(None) == 0  # type: ignore[arg-type]


def test_systematic_cap_25pct() -> None:
    asks = _book(100, 200, 300)  # top-3 = 600
    chk = check_depth(
        side="BUY", requested_shares=200, bids=[], asks=asks,
        cap_pct=DEFAULT_SYSTEMATIC_DEPTH_PCT,
    )
    # Cap = 600 * 0.25 = 150. Requested 200 > 150 → not allowed.
    assert chk.cap_shares == 150
    assert chk.allowed is False
    assert chk.pct_used == pytest.approx(200 / 600, rel=1e-3)


def test_degen_cap_40pct_more_permissive() -> None:
    asks = _book(100, 200, 300)
    chk = check_depth(
        side="BUY", requested_shares=200, bids=[], asks=asks,
        cap_pct=DEFAULT_DEGEN_DEPTH_PCT,
    )
    # Cap = 600 * 0.40 = 240. Requested 200 ≤ 240 → allowed.
    assert chk.cap_shares == 240
    assert chk.allowed is True


def test_cap_size_to_depth_returns_cap_when_exceeded() -> None:
    asks = _book(100, 200, 300)
    capped = cap_size_to_depth(
        500, side="BUY", bids=[], asks=asks,
        cap_pct=DEFAULT_SYSTEMATIC_DEPTH_PCT,
    )
    assert capped == 150


def test_cap_size_to_depth_returns_requested_when_under_cap() -> None:
    asks = _book(100, 200, 300)
    capped = cap_size_to_depth(
        50, side="BUY", bids=[], asks=asks,
        cap_pct=DEFAULT_SYSTEMATIC_DEPTH_PCT,
    )
    assert capped == 50


def test_cap_size_to_depth_zero_when_no_depth() -> None:
    capped = cap_size_to_depth(
        100, side="BUY", bids=[], asks=[],
        cap_pct=DEFAULT_SYSTEMATIC_DEPTH_PCT,
    )
    assert capped == 0


def test_sell_side_uses_bids() -> None:
    bids = _book(50, 50, 50)  # top-3 = 150; cap @ 25% = 37
    chk = check_depth(
        side="SELL", requested_shares=40, bids=bids, asks=[],
        cap_pct=DEFAULT_SYSTEMATIC_DEPTH_PCT,
    )
    assert chk.cap_shares == 37
    assert chk.allowed is False
