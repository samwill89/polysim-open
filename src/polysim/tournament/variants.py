"""Variant definitions — pre-registered strategy configurations.

Each variant is the cohort-copy strategy with different risk-profile
knobs. They share the same cohort discovery + CopyOnCohortLoop;
divergence happens at the executor's gates (sizing, drawdown, stop-loss,
and — for the signal_* variants — the conversation-signal policy).

Adding a research variant requires an entry in ``VARIANTS``. Add its name to
``LIVE_VARIANT_NAMES`` only when it should be seeded into the forward-paper
pool; no database migration is needed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Variant:
    """One row in the tournament's pre-registered variant pool."""

    name: str
    description: str
    base_profile: str  # 'systematic' | 'medium' | 'degen'
    profile_overrides: dict[str, Any] = field(default_factory=dict)
    starting_balance_cents: int = 1_000_000  # $10k per variant

    def merged_profile_dict(self, base: dict[str, Any]) -> dict[str, Any]:
        """Apply variant overrides on top of the base profile dict."""
        merged = dict(base)
        merged.update(self.profile_overrides)
        merged["name"] = self.name
        return merged


VARIANTS: list[Variant] = [
    Variant(
        name="baseline",
        description="Current production default — 2% positions, 40% drawdown cap.",
        base_profile="systematic",
        profile_overrides={
            "max_pct_per_position": 0.02,
            "drawdown_limit_pct": 0.40,
            "stop_loss_enabled": True,
            "stop_loss_pct": 0.50,
        },
    ),
    Variant(
        name="conservative",
        description="Tighter risk: 1% positions, 30% drawdown, 30% stop.",
        base_profile="systematic",
        profile_overrides={
            "max_pct_per_position": 0.01,
            "drawdown_limit_pct": 0.30,
            "stop_loss_enabled": True,
            "stop_loss_pct": 0.30,
        },
    ),
    Variant(
        name="aggressive_size",
        description="Larger 5% positions, 50% drawdown — fewer concurrent positions.",
        base_profile="systematic",
        profile_overrides={
            "max_pct_per_position": 0.05,
            "drawdown_limit_pct": 0.50,
            "max_open_positions": 10,
        },
    ),
    Variant(
        name="tiny_position",
        description="0.5% positions, 60% drawdown — many small bets.",
        base_profile="systematic",
        profile_overrides={
            "max_pct_per_position": 0.005,
            "drawdown_limit_pct": 0.60,
            "max_open_positions": 100,
        },
    ),
    Variant(
        name="medium_kelly_quarter",
        description="Medium profile with Kelly-fractional sizing at 0.25.",
        base_profile="medium",
        profile_overrides={
            "position_sizing_mode": "kelly_fractional",
            "kelly_fraction": 0.25,
            "drawdown_limit_pct": 0.40,
        },
    ),
    Variant(
        name="medium_kelly_half",
        description="Medium profile with more aggressive Kelly at 0.50.",
        base_profile="medium",
        profile_overrides={
            "position_sizing_mode": "kelly_fractional",
            "kelly_fraction": 0.50,
            "drawdown_limit_pct": 0.50,
        },
    ),
    Variant(
        name="degen_concentrated",
        description="Degen: 3 max positions, 50% each, no drawdown gate.",
        base_profile="degen",
        profile_overrides={
            "max_open_positions": 3,
            "max_pct_per_position": 0.50,
            "drawdown_limit_pct": None,
        },
    ),
    Variant(
        name="hold_to_resolution",
        description="Systematic with drawdown gate disabled — never stops, always holds.",
        base_profile="systematic",
        profile_overrides={
            "max_pct_per_position": 0.02,
            "drawdown_limit_pct": None,
            "stop_loss_enabled": False,
            "daily_loss_limit_pct": None,
        },
    ),
    Variant(
        name="tight_stop",
        description="Same as baseline but with a 20% per-position stop-loss.",
        base_profile="systematic",
        profile_overrides={
            "max_pct_per_position": 0.02,
            "drawdown_limit_pct": 0.40,
            "stop_loss_enabled": True,
            "stop_loss_pct": 0.20,
        },
    ),
    Variant(
        name="liquid_tight",
        description=(
            "Tight stop plus copyability gates: source trade >=$10, "
            "entry <=70c, and market daily volume >=$50k."
        ),
        base_profile="systematic",
        profile_overrides={
            "max_pct_per_position": 0.02,
            "drawdown_limit_pct": 0.30,
            "stop_loss_enabled": True,
            "stop_loss_pct": 0.20,
            "min_source_trade_notional_cents": 1_000,
            "max_entry_price_cents": 70,
            "min_market_daily_volume_cents": 5_000_000,
        },
    ),
    Variant(
        name="sports_politics_tight",
        description=(
            "Tight stop on liquid sports/politics specialist flow, "
            "source trade >=$10 and entry <=70c."
        ),
        base_profile="systematic",
        profile_overrides={
            "max_pct_per_position": 0.02,
            "drawdown_limit_pct": 0.30,
            "stop_loss_enabled": True,
            "stop_loss_pct": 0.20,
            "allowed_market_categories": ["sports", "politics"],
            "min_source_trade_notional_cents": 1_000,
            "max_entry_price_cents": 70,
            "min_market_daily_volume_cents": 5_000_000,
        },
    ),
    Variant(
        name="wide_stop",
        description="Same as baseline but with an 80% per-position stop-loss.",
        base_profile="systematic",
        profile_overrides={
            "max_pct_per_position": 0.02,
            "drawdown_limit_pct": 0.40,
            "stop_loss_enabled": True,
            "stop_loss_pct": 0.80,
        },
    ),
    # ── conversation-signal variants (signals package) ──
    # Identical to baseline except for signal_policy, so the tournament
    # cleanly measures the marginal value of public-conversation conviction.
    # Both are inert no-ops until signal rows exist (signals disabled →
    # every lookup returns None → neutral), so seeding them is safe.
    Variant(
        name="signal_sized",
        description=(
            "Baseline + Reddit-attention conviction sizing (0.5x-1.5x per-position size)."
        ),
        base_profile="systematic",
        profile_overrides={
            "max_pct_per_position": 0.02,
            "drawdown_limit_pct": 0.40,
            "stop_loss_enabled": True,
            "stop_loss_pct": 0.50,
            "signal_policy": "size",
        },
    ),
    Variant(
        name="signal_gated",
        description=(
            "Baseline + attention sizing + skip copies into markets whose "
            "public conversation is confidently dead."
        ),
        base_profile="systematic",
        profile_overrides={
            "max_pct_per_position": 0.02,
            "drawdown_limit_pct": 0.40,
            "stop_loss_enabled": True,
            "stop_loss_pct": 0.50,
            "signal_policy": "gate_and_size",
        },
    ),
    # Strategy set v3: clean forward runs with evidence provenance,
    # shared-subject exposure, protocol fees, and verified executable edge.
    Variant(
        name="baseline_verified",
        description=(
            "Baseline sizing with fail-closed catalyst evidence and "
            "shared-subject correlation controls."
        ),
        base_profile="systematic",
        profile_overrides={
            "max_pct_per_position": 0.02,
            "drawdown_limit_pct": 0.40,
            "stop_loss_enabled": True,
            "stop_loss_pct": 0.50,
            "risk_intelligence_policy": "require_verified_edge",
        },
    ),
    Variant(
        name="tight_stop_verified",
        description="Verified-edge baseline with a 20% per-position stop.",
        base_profile="systematic",
        profile_overrides={
            "max_pct_per_position": 0.02,
            "drawdown_limit_pct": 0.40,
            "stop_loss_enabled": True,
            "stop_loss_pct": 0.20,
            "risk_intelligence_policy": "require_verified_edge",
        },
    ),
    Variant(
        name="liquid_verified",
        description=(
            "Verified-edge tight lane with $10 source, 70c entry, and "
            "$50k market-volume copyability gates."
        ),
        base_profile="systematic",
        profile_overrides={
            "max_pct_per_position": 0.02,
            "drawdown_limit_pct": 0.30,
            "stop_loss_enabled": True,
            "stop_loss_pct": 0.20,
            "min_source_trade_notional_cents": 1_000,
            "max_entry_price_cents": 70,
            "min_market_daily_volume_cents": 5_000_000,
            "risk_intelligence_policy": "require_verified_edge",
        },
    ),
    Variant(
        name="sports_politics_verified",
        description=(
            "Verified-edge liquid sports/politics specialist with "
            "shared-subject exposure capped at one position."
        ),
        base_profile="systematic",
        profile_overrides={
            "max_pct_per_position": 0.02,
            "drawdown_limit_pct": 0.30,
            "stop_loss_enabled": True,
            "stop_loss_pct": 0.20,
            "allowed_market_categories": ["sports", "politics"],
            "min_source_trade_notional_cents": 1_000,
            "max_entry_price_cents": 70,
            "min_market_daily_volume_cents": 5_000_000,
            "risk_intelligence_policy": "require_verified_edge",
        },
    ),
]

LIVE_VARIANT_NAMES: tuple[str, ...] = (
    "baseline_verified",
    "tight_stop_verified",
    "liquid_verified",
    "sports_politics_verified",
)


def variants_by_name() -> dict[str, Variant]:
    return {v.name: v for v in VARIANTS}


def live_variants() -> list[Variant]:
    by_name = variants_by_name()
    return [by_name[name] for name in LIVE_VARIANT_NAMES]


__all__ = [
    "LIVE_VARIANT_NAMES",
    "VARIANTS",
    "Variant",
    "live_variants",
    "variants_by_name",
]
