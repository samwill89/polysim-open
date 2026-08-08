"""Shared domain models used across all modules.

These pydantic models are the language of the system — every queue,
SQLite row, and inter-module call speaks in these types.
Spec references: §6 (schema) and §7 (module signatures).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

type Side = Literal["BUY", "SELL"]
type Outcome = Literal["YES", "NO"]
type ResolutionOutcome = Literal["YES", "NO", "INVALID"]
type PositionStatus = Literal["OPEN", "CLOSED", "RESOLVED"]
type InvestigatorVerdict = Literal["INFORMED", "LUCKY", "UNCLEAR"]
type RunMode = Literal["live_paper", "backtest", "replay"]
type CategoryTier = Literal["primary", "secondary", "pilot"]

# Canonical category names — must match config.categories keys and scoring behaviour.
type CategoryName = Literal[
    "ai",
    "aec",
    "creator",
    "geopolitics",
    "politics",       # added 2026-04-28 — top-2 alpha-bearing per cohort P&L analysis
    "sports",         # added 2026-04-28 — top-1 alpha-bearing per cohort P&L analysis
    "pop_culture",
    "box_office",
    "macro_housing",
    "tech_ma",
    "crypto_launches",
    "gov_defense",
    "other",
]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class TradeEvent(_Frozen):
    """A single filled trade observed on Polymarket."""

    id: str
    wallet_address: str
    market_id: str
    side: Side
    outcome: Outcome
    size_shares: int = Field(ge=0)
    price_cents: int = Field(ge=0, le=100)
    timestamp: datetime
    tx_hash: str | None = None


class Market(_Frozen):
    """Polymarket market metadata."""

    id: str
    slug: str
    question: str
    category: CategoryName | None = None
    created_at: datetime
    resolves_at: datetime | None = None
    resolved_outcome: ResolutionOutcome | None = None
    resolved_at: datetime | None = None
    daily_volume_usd_cents: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class Wallet(_Frozen):
    """On-chain wallet metadata + Polymarket display info."""

    address: str
    first_seen_at: datetime
    nonce: int | None = None
    funding_source: str | None = None
    funding_first_deposit_at: datetime | None = None
    lifetime_volume_cents: int = 0
    lifetime_trades: int = 0
    display_name: str | None = None
    # G1 — Polymarket proxy-wallet resolution. When None, address IS the owner.
    owner_address: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    last_profiled_at: datetime | None = None


class WalletProfile(_Frozen):
    """Recomputed per-wallet snapshot. Idempotent (spec §7.2)."""

    wallet_address: str
    as_of: datetime
    total_markets: int = 0
    resolved_markets: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    total_pnl_cents: int = 0
    categories: dict[str, int] = Field(default_factory=dict)
    category_exclusivity: float = 0.0  # 0..1 Herfindahl
    avg_entry_to_resolution_hours: float = 0.0
    features: dict[str, Any] = Field(default_factory=dict)


class DetectorSignal(_Frozen):
    """One detector's score for one (wallet, market) pair. Spec §7.3."""

    wallet_address: str
    market_id: str
    trade_id: str | None
    detector_name: str
    raw_score: float = Field(ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    components: dict[str, float] = Field(default_factory=dict)
    evidence: dict[str, Any] = Field(default_factory=dict)


class CompositeScore(_Frozen):
    """Composite of all detector signals. Spec §7.4."""

    wallet_address: str
    market_id: str
    score: float = Field(ge=0.0, le=10.0)
    components: dict[str, float] = Field(default_factory=dict)
    contributing_detectors: list[str] = Field(default_factory=list)
    created_at: datetime


class Flag(BaseModel):
    """Persisted flag row. Spec §6 `flags` table."""

    model_config = ConfigDict(extra="forbid")

    id: int | None = None
    wallet_address: str
    market_id: str
    trade_id: str | None = None
    detector_name: str
    raw_score: float
    composite_score: float | None = None
    components: dict[str, Any]
    investigator_verdict: InvestigatorVerdict | None = None
    investigator_reasoning: str | None = None
    created_at: datetime
    acted_on: bool = False


class InvestigatorOutcome(_Frozen):
    """Claude's verdict on a flag. Spec §7.5. Filter only, not scorer."""

    verdict: InvestigatorVerdict
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str
    red_flags: list[str] = Field(default_factory=list)
    green_flags: list[str] = Field(default_factory=list)


class PaperRun(BaseModel):
    """A paper-trading simulation run. Spec §6 `paper_runs` table."""

    model_config = ConfigDict(extra="forbid")

    id: int | None = None
    name: str
    started_at: datetime
    ended_at: datetime | None = None
    config_snapshot: dict[str, Any]
    starting_balance_cents: int
    current_balance_cents: int
    notes: str | None = None


class PaperPosition(BaseModel):
    """One paper-trading position. Spec §6 `paper_positions` table."""

    model_config = ConfigDict(extra="forbid")

    id: int | None = None
    run_id: int
    market_id: str
    outcome: Outcome
    size_shares: int
    avg_entry_price_cents: int
    opened_at: datetime
    closed_at: datetime | None = None
    realized_pnl_cents: int | None = None
    source_flag_id: int | None = None
    source_wallet: str | None = None
    status: PositionStatus


class PaperFill(BaseModel):
    """One paper-trading fill event. Spec §6 `paper_fills` table."""

    model_config = ConfigDict(extra="forbid")

    id: int | None = None
    run_id: int
    position_id: int
    side: Side
    size_shares: int
    fill_price_cents: int
    intended_price_cents: int
    slippage_cents: int
    latency_ms: int
    fee_cents: int
    timestamp: datetime
