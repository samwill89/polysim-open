-- Empirical-priors addendum §3 + §4 + §5 data model.
-- Adds:
--   wallets                 : normalized wallet registry + edge_likelihood scores
--   wallet_features         : per-(wallet, as_of) feature snapshot (Polars-produced)
--   experiments             : frozen-cohort container; version + hash per experiment
--   decision_rejections     : gate-by-gate breakdown of every rejected trade (§5.4)
--   settlement_events       : buy_ts → settled_ts per position (§4.2)
--   flags.resolution_risk_score  : §4.3
--   paper_positions.pending_until_iso : §4.2 capacity lock

-- The `wallets` table coexists with `wallet_profiles` from migration 0001.
-- wallet_profiles = time-series snapshots of trading behavior (used by detectors).
-- wallets        = canonical registry + cohort membership + classifier output.
CREATE TABLE IF NOT EXISTS wallets_discovery (
    address TEXT PRIMARY KEY,
    first_seen_at TEXT NOT NULL,
    nonce INTEGER,
    funding_source TEXT,
    lifetime_volume_cents INTEGER NOT NULL DEFAULT 0,
    lifetime_trades INTEGER NOT NULL DEFAULT 0,
    is_cohort INTEGER NOT NULL DEFAULT 0,         -- 0/1
    cohort_niche TEXT,                             -- 'aec' | 'ai_labs' | 'creator_econ' | 'general' | NULL
    experiment_id INTEGER,                         -- FK -> experiments.id once frozen
    edge_likelihood_global REAL,                   -- Tier 1 classifier output
    edge_likelihood_aec REAL,
    edge_likelihood_ai_labs REAL,
    edge_likelihood_creator_econ REAL,
    last_classified_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_wallets_disc_cohort ON wallets_discovery(is_cohort, cohort_niche);
CREATE INDEX IF NOT EXISTS idx_wallets_disc_experiment ON wallets_discovery(experiment_id);

CREATE TABLE IF NOT EXISTS wallet_features (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet_address TEXT NOT NULL,
    as_of TEXT NOT NULL,
    scope TEXT NOT NULL,                           -- 'global' | 'aec' | 'ai_labs' | 'creator_econ'
    win_rate REAL,
    trade_count INTEGER,
    avg_hold_hours REAL,
    early_exit_ratio REAL,
    avg_size_vs_depth REAL,
    counterparty_concentration REAL,
    pnl_lifetime_cents INTEGER,
    pnl_30d_cents INTEGER,
    categories_json TEXT NOT NULL DEFAULT '{}',
    niche_mix_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(wallet_address, as_of, scope)
);
CREATE INDEX IF NOT EXISTS idx_wallet_features_scope ON wallet_features(scope, as_of DESC);
CREATE INDEX IF NOT EXISTS idx_wallet_features_addr ON wallet_features(wallet_address, as_of DESC);

-- Pre-registered experiments with frozen cohorts.
CREATE TABLE IF NOT EXISTS experiments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,                     -- 'experiment_001'
    version TEXT NOT NULL,                         -- niche-tag version + schema version
    niche_tags_version TEXT NOT NULL,              -- bump breaks cohort
    belief_schema_version TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    cohort_frozen_at TEXT,                         -- ISO or NULL if still forming
    cohort_hash TEXT,                              -- SHA256 of sorted wallet addresses
    cohort_size INTEGER,
    notes TEXT
);

-- Every rejected trade logged with full gate breakdown (§5.4).
CREATE TABLE IF NOT EXISTS decision_rejections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id TEXT,
    flag_id INTEGER,                               -- FK -> flags.id, nullable if no flag
    market_id TEXT,
    belief_json TEXT,                              -- the investigator's full Belief
    gate_confidence INTEGER NOT NULL DEFAULT 0,    -- 0/1 pass
    gate_resolution_risk INTEGER NOT NULL DEFAULT 0,
    gate_ev INTEGER NOT NULL DEFAULT 0,
    gate_directional INTEGER NOT NULL DEFAULT 0,
    gate_depth INTEGER NOT NULL DEFAULT 0,
    gate_concentration INTEGER NOT NULL DEFAULT 0,
    rejection_reason TEXT NOT NULL,
    rejected_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rejections_cycle ON decision_rejections(cycle_id);
CREATE INDEX IF NOT EXISTS idx_rejections_market ON decision_rejections(market_id);

-- Settlement cycle tracking (§4.2).
CREATE TABLE IF NOT EXISTS settlement_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id INTEGER NOT NULL REFERENCES paper_positions(id),
    buy_ts TEXT NOT NULL,
    settled_ts TEXT,                               -- NULL while pending
    pending_until TEXT NOT NULL,
    blocks_to_settle INTEGER NOT NULL DEFAULT 2,
    capacity_locked_cents INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_settlement_position ON settlement_events(position_id);
CREATE INDEX IF NOT EXISTS idx_settlement_pending ON settlement_events(settled_ts);

-- §4.3 resolution risk score on flags.
ALTER TABLE flags ADD COLUMN resolution_risk_score REAL;

-- §4.2 capacity lock state on positions.
ALTER TABLE paper_positions ADD COLUMN pending_until_iso TEXT;
