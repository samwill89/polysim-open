"""Configuration loader — YAML for app config, env-vars for secrets.

Spec refs: §11 config schema, §14 #2 (no private keys), §18 (operator decisions).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# ── Sub-configs ────────────────────────────────────────────────


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RunConfig(_Strict):
    name: str
    mode: Literal["live_paper", "backtest", "replay"] = "live_paper"
    starting_balance_cents: int = 1_000_000  # $10,000 per §18 Q1


class BankrollConfig(_Strict):
    max_open_positions: int = 20
    max_pct_per_position: float = 0.02
    max_pct_per_source_wallet: float = 0.10
    max_pct_per_market: float = 0.05
    fixed_copy_cents: int = 5_000
    # G9 — size = base * clamp(composite/divisor, floor, ceiling).
    copy_size_divisor: float = 5.0
    copy_size_floor: float = 0.5
    copy_size_ceiling: float = 2.0
    # G10 — optional mirror-exits on source-wallet sells.
    mirror_exits: bool = False
    # §14 #7 kill switches.
    kill_drawdown_pct: float = 0.20
    kill_flags_per_hour: int = 100


class ScoringWeights(_Strict):
    CategoryInsiderDetector: float = 3.5
    EventInsiderDetector: float = 2.5
    TimingDetector: float = 1.5
    CoordinationDetector: float = 1.5
    FreshWalletDetector: float = 1.0


class ScoringConfig(_Strict):
    flag_threshold: float = 5.0
    min_contributing_detectors: int = 2
    weights: ScoringWeights = Field(default_factory=ScoringWeights)


class _CategoryInsiderParams(_Strict):
    min_resolved_markets: int = 8


class _EventInsiderParams(_Strict):
    fresh_nonce_threshold: int = 10
    fresh_size_min_cents: int = 500_000
    niche_market_vol_max_cents: int = 50_000_000
    contrarian_bps: int = 1_500


class _CoordinationParams(_Strict):
    window_hours: int = 24  # G4 — default 24h, config-tunable


class _TimingParams(_Strict):
    late_window_hours: int = 1


class DetectorParams(_Strict):
    category_insider: _CategoryInsiderParams = Field(default_factory=_CategoryInsiderParams)
    event_insider: _EventInsiderParams = Field(default_factory=_EventInsiderParams)
    coordination: _CoordinationParams = Field(default_factory=_CoordinationParams)
    timing: _TimingParams = Field(default_factory=_TimingParams)


class InvestigatorConfig(_Strict):
    enabled: bool = True
    model: str = "claude-opus-4-7"
    triage_model: str = "claude-haiku-4-5"
    min_composite_to_invoke: float = 6.0
    min_verdict_confidence_to_act: float = 0.6
    max_calls_per_day: int = 100


class FillModelConfig(_Strict):
    detection_latency_p50_ms: int = 3_000
    detection_latency_p95_ms: int = 10_000
    decision_latency_p50_ms: int = 2_000
    decision_latency_p95_ms: int = 15_000
    slippage_ticks: int = 1
    on_partial: Literal["ABANDON", "CHASE"] = "ABANDON"
    fee_bps: int = 0
    historical_pessimism_multiplier: float = 2.0  # G8


class CategoryEntry(_Strict):
    enabled: bool
    tier: Literal["primary", "secondary", "pilot"]
    keywords: list[str] = Field(default_factory=list)
    examples: list[str] = Field(default_factory=list)
    note: str | None = None


class KnownInsider(_Strict):
    label: str
    address: str = ""


class TelegramConfig(_Strict):
    enabled: bool = True
    bot_token: str = "${TELEGRAM_BOT_TOKEN}"
    chat_id: str = "${TELEGRAM_CHAT_ID}"


class LoggingConfig(_Strict):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    json_file: str | None = "./logs/polysim.jsonl"
    rotate_bytes: int = 10_485_760
    keep_files: int = 14


# ── RiskProfile (addendum §2) ──────────────────────────────────

PositionSizingMode = Literal["fixed", "percentage", "kelly_fractional"]


class RiskProfile(_Strict):
    """Risk profile for a single paper run. Addendum §2.

    Snapshotted at run start into paper_runs.profile_snapshot_json; mid-run
    edits to the source YAML do NOT retroactively affect historical runs.
    """

    name: str
    description: str

    # Position sizing
    position_sizing_mode: PositionSizingMode
    fixed_copy_cents: int | None = None
    max_pct_per_position: float
    kelly_fraction: float | None = None

    # Portfolio caps
    max_open_positions: int
    max_pct_per_source_wallet: float
    max_pct_per_market: float

    # Market filters
    min_market_odds: float | None = None
    max_market_odds: float | None = None
    max_market_daily_volume_cents: int | None = None
    min_market_daily_volume_cents: int | None = None
    allowed_market_categories: list[str] | None = None
    min_source_trade_notional_cents: int | None = None
    max_entry_price_cents: int | None = None

    # Signal filters
    allowed_detectors: list[str] | None = None
    min_composite_score: float

    # Risk management
    stop_loss_enabled: bool
    stop_loss_pct: float | None = None
    drawdown_limit_pct: float | None = None
    daily_loss_limit_pct: float | None = None

    # Compounding behavior
    compound_winnings: bool

    # External conversation signal (signals package). Defaults are all
    # "off"/neutral so historical profile snapshots and the three built-in
    # YAMLs keep their exact behavior. `size` multiplies position size by a
    # bounded conviction factor; `gate_and_size` additionally skips copies
    # into markets whose public conversation is confidently dead.
    signal_policy: Literal["off", "size", "gate_and_size"] = "off"
    signal_max_age_hours: float = 24.0
    signal_size_min_mult: float = 0.5
    signal_size_max_mult: float = 1.5
    signal_gate_min_composite: float = 0.15
    signal_gate_min_confidence: float = 0.5

    # Evidence-aware execution controls. `require_verified_edge` fails closed
    # only for catalyst-sensitive markets; ordinary markets retain the cohort
    # copy path. Correlation limits apply whenever this policy is not `off`.
    risk_intelligence_policy: Literal["off", "monitor", "require_verified_edge"] = "off"
    risk_min_source_quality: float = Field(default=0.60, ge=0.0, le=1.0)
    risk_max_rumor_risk: float = Field(default=0.50, ge=0.0, le=1.0)
    risk_max_information_asymmetry: float = Field(default=0.65, ge=0.0, le=1.0)
    risk_min_probability_confidence: float = Field(default=0.65, ge=0.0, le=1.0)
    risk_min_edge_after_cost_cents: float = Field(default=2.0, ge=0.0, le=100.0)
    risk_uncertainty_penalty_max_cents: float = Field(default=10.0, ge=0.0, le=100.0)
    max_correlated_positions_per_subject: int = Field(default=1, ge=1, le=100)
    max_subject_exposure_pct: float = Field(default=0.02, ge=0.0, le=1.0)

    @field_validator("position_sizing_mode")
    @classmethod
    def _sizing_mode_requires_fields(cls, v: str) -> str:
        # pydantic runs field validators before model cross-checks; we enforce
        # cross-field requirements in a model validator below.
        return v

    def ensure_sizing_fields(self) -> None:
        """Validate cross-field invariants that depend on sizing mode."""
        if self.position_sizing_mode == "fixed" and self.fixed_copy_cents is None:
            raise ValueError("profile mode 'fixed' requires fixed_copy_cents")
        if self.position_sizing_mode == "kelly_fractional" and self.kelly_fraction is None:
            raise ValueError("profile mode 'kelly_fractional' requires kelly_fraction")


class IntelSourceSet(_Strict):
    """Per-niche intel sources: Reddit subs, Telegram channels, X accounts.

    X entries are scaffolded but currently inert — we have no polling path
    without the paid API. Listing them here is documentation + makes the
    upgrade a one-line CLI flip when an API key is added.
    """

    reddit: list[str] = Field(default_factory=list)
    telegram: list[str] = Field(default_factory=list)
    x: list[str] = Field(default_factory=list)


class SignalsConfig(_Strict):
    """External conversation-signal layer (signals package). §addendum.

    Disabled by default; no credentials required. When `category_subreddits`
    is empty the map is derived from `intel_sources` reddit lists (which
    already cover every niche, e.g. box_office → r/boxoffice).
    """

    enabled: bool = False
    provider: Literal["reddit", "fixture", "none"] = "reddit"
    fixtures_dir: str | None = None  # required when provider == 'fixture'
    window_hours: float = 24.0  # activity window per snapshot
    min_baseline_snapshots: int = 5  # history rows needed for a z-score
    min_matched_posts: int = 3  # matched posts for full confidence
    request_timeout_s: float = 10.0
    snapshot_interval_s: float = 3600.0  # live loop cadence (1h)
    max_markets_per_snapshot: int = 40  # bound work per tick
    max_subreddits_per_category: int = 3
    category_subreddits: dict[str, list[str]] = Field(default_factory=dict)


class EvidenceConfig(_Strict):
    """Credential-free news evidence used by the pre-trade risk gate."""

    enabled: bool = False
    provider: Literal["gdelt", "none"] = "gdelt"
    request_timeout_s: float = Field(default=10.0, gt=0.0, le=60.0)
    max_age_hours: float = Field(default=6.0, gt=0.0, le=168.0)
    lookback_days: int = Field(default=7, ge=1, le=30)
    max_articles: int = Field(default=30, ge=1, le=75)


class IngestConfig(_Strict):
    polymarket_ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/live-activity"
    polymarket_gamma_url: str = "https://gamma-api.polymarket.com"
    polymarket_data_url: str = "https://data-api.polymarket.com"
    polymarket_clob_url: str = "https://clob.polymarket.com"
    polygon_rpc_url: str = "https://polygon-mainnet.g.alchemy.com/v2/{api_key}"
    polygon_rps_cap: int = 10
    funding_sources_path: str = "./configs/funding_sources.yml"
    market_indexer_interval_s: int = 3600
    trade_persist_batch_size: int = 50
    trade_persist_max_latency_s: float = 2.0


# ── Root config ────────────────────────────────────────────────


class Config(_Strict):
    run: RunConfig
    bankroll: BankrollConfig = Field(default_factory=BankrollConfig)
    scoring: ScoringConfig = Field(default_factory=ScoringConfig)
    detectors: DetectorParams = Field(default_factory=DetectorParams)
    investigator: InvestigatorConfig = Field(default_factory=InvestigatorConfig)
    fill_model: FillModelConfig = Field(default_factory=FillModelConfig)
    categories: dict[str, CategoryEntry]
    known_insiders: list[KnownInsider] = Field(default_factory=list)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    ingest: IngestConfig = Field(default_factory=IngestConfig)
    intel_sources: dict[str, IntelSourceSet] = Field(default_factory=dict)
    signals: SignalsConfig = Field(default_factory=SignalsConfig)
    evidence: EvidenceConfig = Field(default_factory=EvidenceConfig)

    @field_validator("run")
    @classmethod
    def _reject_live_trading(cls, v: RunConfig) -> RunConfig:
        # Spec §14 #3 — Literal[...] already enforces this, but we keep an
        # explicit guard so the rule remains obvious in grep/audit.
        if str(v.mode) == "live_trading":
            raise ValueError("mode 'live_trading' is banned by spec §14 #3")
        return v


# ── Secrets from environment ───────────────────────────────────


class Secrets(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )
    ANTHROPIC_API_KEY: str = ""
    ALCHEMY_API_KEY: str = ""
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_CHAT_ID: str = ""
    # Tier-3 user-account scraper (MTProto). Get from https://my.telegram.org/apps
    TELEGRAM_API_ID: str = ""
    TELEGRAM_API_HASH: str = ""
    POLYSIM_DB_PATH: str = "./polysim.db"
    POLYSIM_CONFIG_PATH: str = "./config.yml"
    POLYSIM_LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"


# ── Loader helpers ─────────────────────────────────────────────


def load_config(path: Path | str | None = None) -> Config:
    """Load YAML config, reject private-key fields (§14 #2)."""
    if path is None:
        path = Path(load_secrets().POLYSIM_CONFIG_PATH)
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"Config file not found: {path}. "
            "Run `polysim init` or copy config.example.yml to config.yml."
        )
    with path.open("r", encoding="utf-8") as f:
        raw: Any = yaml.safe_load(f)
    _assert_no_private_key(raw)
    return Config.model_validate(raw)


def load_secrets() -> Secrets:
    return Secrets()


def _assert_no_private_key(obj: Any) -> None:
    """Recursively reject any key containing 'private_key'. Spec §14 #2."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if "private_key" in str(k).lower():
                raise ValueError(
                    "Config contains a 'private_key' field. This is forbidden by spec §14 #2."
                )
            _assert_no_private_key(v)
    elif isinstance(obj, list):
        for x in obj:
            _assert_no_private_key(x)
