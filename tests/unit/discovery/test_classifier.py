"""Tier-1 classifier — empirical-priors addendum §5.1.

Locks in:
  * scores in [0, 1]
  * stable under feature-order permutation (§9.3 property test)
  * higher win_rate + higher pnl → higher score (monotonicity)
  * scope-relative ranking (a wallet good in AEC peers can outrank a
    globally-strong wallet within its own pool)
"""

from __future__ import annotations

import random
from datetime import UTC, datetime

import pytest

from polysim.discovery.classifier import (
    CLASSIFIER_VERSION,
    classify_population,
)
from polysim.discovery.features import WalletFeatures


def _wf(
    addr: str, scope: str, *,
    win_rate: float = 0.5,
    trade_count: int = 100,
    pnl: int = 0,
    early_exit: float = 0.0,
    cc: float = 0.0,
) -> WalletFeatures:
    return WalletFeatures(
        wallet_address=addr, scope=scope,
        win_rate=win_rate, trade_count=trade_count,
        avg_hold_hours=12.0, early_exit_ratio=early_exit,
        avg_size_vs_depth=0.0, counterparty_concentration=cc,
        pnl_lifetime_cents=pnl, pnl_30d_cents=0,
        category_mix={}, niche_mix={},
        as_of=datetime.now(UTC),
    )


def test_version_constant() -> None:
    assert CLASSIFIER_VERSION == "1.0.0"


def test_scores_within_unit_interval() -> None:
    pop = [
        _wf("0x1", "global", win_rate=0.7, pnl=50_000),
        _wf("0x2", "global", win_rate=0.4, pnl=-10_000),
        _wf("0x3", "global", win_rate=0.55, pnl=100),
    ]
    scores = classify_population(pop)
    for s in scores:
        assert 0.0 <= s.edge_likelihood <= 1.0


def test_higher_pnl_higher_score_holding_else_equal() -> None:
    pop = [
        _wf("0xA", "global", win_rate=0.6, pnl=1_000_000, trade_count=200),
        _wf("0xB", "global", win_rate=0.6, pnl=-1_000_000, trade_count=200),
    ]
    scores = {s.wallet_address: s.edge_likelihood for s in classify_population(pop)}
    assert scores["0xA"] > scores["0xB"]


def test_stable_under_input_permutation() -> None:
    """Property: the score of any wallet is invariant to the order of
    the input list (classifier should not implicitly use row order)."""
    rng = random.Random(42)
    base = [
        _wf(f"0x{i:02x}", "global", win_rate=0.4 + (i % 5) * 0.05,
            trade_count=50 + i * 3, pnl=(i - 5) * 1000)
        for i in range(20)
    ]
    a = {s.wallet_address: round(s.edge_likelihood, 6)
         for s in classify_population(base)}
    shuffled = list(base)
    rng.shuffle(shuffled)
    b = {s.wallet_address: round(s.edge_likelihood, 6)
         for s in classify_population(shuffled)}
    assert a == b


def test_per_scope_normalization() -> None:
    """A wallet that's median in 'global' but top-of-AEC should rank
    higher in AEC than in global (because z-score is per-scope)."""
    aec_pop = [
        _wf("0xtop", "aec", win_rate=0.95, pnl=500_000, trade_count=80),
        _wf("0xmid", "aec", win_rate=0.5, pnl=0, trade_count=80),
        _wf("0xlow", "aec", win_rate=0.2, pnl=-500_000, trade_count=80),
    ]
    global_pop = [
        _wf("0xtop", "global", win_rate=0.6, pnl=10_000, trade_count=80),
    ] + [
        _wf(f"0xother{i}", "global", win_rate=0.7, pnl=200_000, trade_count=200)
        for i in range(20)
    ]
    scored = classify_population(aec_pop + global_pop)
    by = {(s.wallet_address, s.scope): s.edge_likelihood for s in scored}
    # In AEC, '0xtop' is best of 3 → high score.
    # In global, '0xtop' is below median → low score.
    assert by[("0xtop", "aec")] > by[("0xtop", "global")]


@pytest.mark.parametrize("scope", ["global", "aec", "ai_labs", "creator_econ"])
def test_empty_population_returns_empty(scope: str) -> None:
    assert classify_population([]) == []
    _ = scope
