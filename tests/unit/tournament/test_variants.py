"""Variant-config sanity tests."""

from __future__ import annotations

from polysim.config import RiskProfile
from polysim.profiles import load_profile
from polysim.tournament.variants import (
    LIVE_VARIANT_NAMES,
    VARIANTS,
    Variant,
    live_variants,
    variants_by_name,
)


def test_eighteen_variants_registered() -> None:
    assert len(VARIANTS) == 18


def test_variant_names_unique() -> None:
    names = [v.name for v in VARIANTS]
    assert len(names) == len(set(names))


def test_each_variant_targets_known_profile() -> None:
    for v in VARIANTS:
        assert v.base_profile in {"systematic", "medium", "degen"}


def test_lookup_by_name() -> None:
    by_name = variants_by_name()
    assert "baseline" in by_name
    assert by_name["baseline"].base_profile == "systematic"


def test_live_pool_is_compact_and_explicit() -> None:
    assert [variant.name for variant in live_variants()] == list(LIVE_VARIANT_NAMES)
    assert set(LIVE_VARIANT_NAMES) == {
        "baseline_verified",
        "tight_stop_verified",
        "liquid_verified",
        "sports_politics_verified",
    }


def test_live_variants_require_verified_edge() -> None:
    for variant in live_variants():
        assert variant.profile_overrides["risk_intelligence_policy"] == "require_verified_edge"


def test_selective_variants_have_copyability_gates() -> None:
    by_name = variants_by_name()
    for name in ("liquid_tight", "sports_politics_tight"):
        overrides = by_name[name].profile_overrides
        assert overrides["max_entry_price_cents"] == 70
        assert overrides["min_source_trade_notional_cents"] == 1_000
        assert overrides["min_market_daily_volume_cents"] == 5_000_000
    assert "allowed_market_categories" not in by_name["liquid_tight"].profile_overrides
    assert by_name["sports_politics_tight"].profile_overrides["allowed_market_categories"] == [
        "sports",
        "politics",
    ]


def test_merged_profile_applies_overrides() -> None:
    v = Variant(
        name="x",
        description="t",
        base_profile="systematic",
        profile_overrides={"max_pct_per_position": 0.10},
    )
    merged = v.merged_profile_dict({"max_pct_per_position": 0.02, "other": 1})
    assert merged["max_pct_per_position"] == 0.10
    assert merged["other"] == 1
    assert merged["name"] == "x"


# ── conversation-signal variants ─────────────────────────


def test_signal_variants_registered_with_policies() -> None:
    by_name = variants_by_name()
    assert by_name["signal_sized"].profile_overrides["signal_policy"] == "size"
    assert by_name["signal_gated"].profile_overrides["signal_policy"] == "gate_and_size"


def test_signal_variants_match_baseline_risk_knobs() -> None:
    """The A/B is only clean if signal_policy is the sole difference."""
    by_name = variants_by_name()
    base = dict(by_name["baseline"].profile_overrides)
    for name in ("signal_sized", "signal_gated"):
        overrides = dict(by_name[name].profile_overrides)
        overrides.pop("signal_policy")
        assert overrides == base, name


def test_every_variant_snapshot_validates_as_risk_profile() -> None:
    """Seeding writes merged_profile_dict(base) as the run's snapshot; a
    bad override key would crash live startup when executors are rebuilt
    from snapshots (RiskProfile is extra='forbid')."""
    for v in VARIANTS:
        base = load_profile(v.base_profile)
        snap = v.merged_profile_dict(base.model_dump())
        profile = RiskProfile.model_validate(snap)
        assert profile.name == v.name


def test_default_profiles_have_signal_policy_off() -> None:
    for name in ("systematic", "medium", "degen"):
        assert load_profile(name).signal_policy == "off"
