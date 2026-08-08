"""Frozen-cohort selection — empirical-priors addendum §3.4 + §3.5."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from polysim.db.migrations.runner import apply_migrations
from polysim.discovery.classifier import ClassifierScore
from polysim.discovery.cohort import (
    cohort_hash,
    freeze_cohort,
    select_cohort,
)
from polysim.discovery.features import WalletFeatures


def _wf(
    addr: str, scope: str, *,
    trade_count: int = 100,
) -> WalletFeatures:
    return WalletFeatures(
        wallet_address=addr, scope=scope,
        win_rate=0.55, trade_count=trade_count,
        avg_hold_hours=12.0, early_exit_ratio=0.0,
        avg_size_vs_depth=0.0, counterparty_concentration=0.0,
        pnl_lifetime_cents=0, pnl_30d_cents=0,
        category_mix={}, niche_mix={},
        as_of=datetime.now(UTC),
    )


def test_select_cohort_dedups_primary_into_general() -> None:
    """If a wallet is selected into a niche pool, it must NOT appear again
    in the general pool — secondary is general-MINUS-primary."""
    scores = [
        ClassifierScore("0xA", "aec", 0.95, {}),
        ClassifierScore("0xA", "global", 0.95, {}),
        ClassifierScore("0xB", "global", 0.80, {}),
    ]
    feats = {("0xA", "aec"): _wf("0xA", "aec", trade_count=80)}
    picks = select_cohort(
        scores, feats,
        per_niche_target=10, general_target=10, min_niche_trades=50,
    )
    addrs = [p.wallet_address for p in picks]
    pools = {p.wallet_address: p.pool for p in picks}
    # 0xA is in primary (aec) and must not be in general.
    assert addrs.count("0xA") == 1
    assert pools["0xA"] == "aec"
    assert "0xB" in addrs
    assert pools["0xB"] == "general"


def test_select_cohort_enforces_min_niche_trades() -> None:
    scores = [ClassifierScore("0xLowVol", "aec", 0.99, {})]
    feats = {("0xLowVol", "aec"): _wf("0xLowVol", "aec", trade_count=10)}
    picks = select_cohort(
        scores, feats,
        per_niche_target=5, general_target=0, min_niche_trades=50,
    )
    assert picks == []


def test_select_cohort_caps_per_pool() -> None:
    # 5 candidates in aec, request top 2.
    scores = [
        ClassifierScore(f"0x{i}", "aec", 0.9 - i * 0.05, {})
        for i in range(5)
    ]
    feats = {(f"0x{i}", "aec"): _wf(f"0x{i}", "aec", trade_count=80) for i in range(5)}
    picks = select_cohort(
        scores, feats,
        per_niche_target=2, general_target=0, min_niche_trades=50,
    )
    assert len(picks) == 2
    # Highest-scoring two.
    assert {p.wallet_address for p in picks} == {"0x0", "0x1"}


def test_cohort_hash_stable_and_unique() -> None:
    from polysim.discovery.cohort import CohortPick
    a = [
        CohortPick("0xA", "aec", 0.9),
        CohortPick("0xB", "general", 0.8),
    ]
    b = [
        CohortPick("0xb", "general", 0.8),     # case-insensitive
        CohortPick("0xa", "aec", 0.9),         # order-invariant
    ]
    c = [
        CohortPick("0xA", "aec", 0.9),
        CohortPick("0xC", "general", 0.8),     # different members
    ]
    assert cohort_hash(a) == cohort_hash(b)
    assert cohort_hash(a) != cohort_hash(c)


@pytest.fixture
async def db(tmp_path: Path) -> Path:
    p = tmp_path / "t.db"
    await apply_migrations(p)
    return p


async def test_freeze_cohort_round_trip(db: Path) -> None:
    from polysim.agents.belief_schema import SCHEMA_VERSION
    from polysim.discovery.classifier import CLASSIFIER_VERSION
    from polysim.discovery.cohort import CohortPick

    picks = [
        CohortPick("0xA", "aec", 0.95),
        CohortPick("0xB", "general", 0.70),
    ]
    eid = await freeze_cohort(
        db,
        experiment_name="exp_test",
        picks=picks,
        classifier_version=CLASSIFIER_VERSION,
        belief_schema_version=SCHEMA_VERSION,
    )
    assert eid > 0
    # Re-running with the same name updates in place, doesn't duplicate.
    eid2 = await freeze_cohort(
        db,
        experiment_name="exp_test",
        picks=picks,
        classifier_version=CLASSIFIER_VERSION,
        belief_schema_version=SCHEMA_VERSION,
    )
    assert eid2 == eid
