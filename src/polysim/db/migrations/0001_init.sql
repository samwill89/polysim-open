-- PolySim initial schema — spec §6 + Phase-0 additions (G1, G3, G7, §18 Q5).
-- All timestamps UTC ISO 8601 TEXT.  All money stored as integer USDC cents.
-- NEVER EDIT THIS FILE POST-RELEASE — add a new numbered migration instead.

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

-- Markets
CREATE TABLE markets (
    id TEXT PRIMARY KEY,
    slug TEXT NOT NULL,
    question TEXT NOT NULL,
    category TEXT,
    created_at TEXT NOT NULL,
    resolves_at TEXT,
    resolved_outcome TEXT,
    resolved_at TEXT,
    daily_volume_usd_cents INTEGER,
    metadata_json TEXT
);
CREATE INDEX idx_markets_category ON markets(category);
CREATE INDEX idx_markets_resolves_at ON markets(resolves_at);

-- Wallets (owner_address = G1 proxy resolution)
CREATE TABLE wallets (
    address TEXT PRIMARY KEY,
    first_seen_at TEXT NOT NULL,
    nonce INTEGER,
    funding_source TEXT,
    funding_first_deposit_at TEXT,
    lifetime_volume_cents INTEGER NOT NULL DEFAULT 0,
    lifetime_trades INTEGER NOT NULL DEFAULT 0,
    display_name TEXT,
    owner_address TEXT,
    metadata_json TEXT,
    last_profiled_at TEXT
);
CREATE INDEX idx_wallets_owner ON wallets(owner_address);

-- Trades
CREATE TABLE trades (
    id TEXT PRIMARY KEY,
    wallet_address TEXT NOT NULL REFERENCES wallets(address),
    market_id TEXT NOT NULL REFERENCES markets(id),
    side TEXT NOT NULL CHECK(side IN ('BUY', 'SELL')),
    outcome TEXT NOT NULL CHECK(outcome IN ('YES', 'NO')),
    size_shares INTEGER NOT NULL,
    price_cents INTEGER NOT NULL CHECK(price_cents BETWEEN 0 AND 100),
    timestamp TEXT NOT NULL,
    tx_hash TEXT
);
CREATE INDEX idx_trades_wallet ON trades(wallet_address, timestamp);
CREATE INDEX idx_trades_market ON trades(market_id, timestamp);
CREATE INDEX idx_trades_timestamp ON trades(timestamp);

-- Wallet profile snapshots
CREATE TABLE wallet_profiles (
    wallet_address TEXT NOT NULL REFERENCES wallets(address),
    as_of TEXT NOT NULL,
    total_markets INTEGER,
    resolved_markets INTEGER,
    wins INTEGER,
    losses INTEGER,
    win_rate REAL,
    total_pnl_cents INTEGER,
    categories_json TEXT,
    category_exclusivity REAL,
    avg_entry_to_resolution_hours REAL,
    features_json TEXT,
    PRIMARY KEY (wallet_address, as_of)
);

-- Flags
CREATE TABLE flags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet_address TEXT NOT NULL,
    market_id TEXT NOT NULL,
    trade_id TEXT,
    detector_name TEXT NOT NULL,
    raw_score REAL NOT NULL,
    composite_score REAL,
    components_json TEXT NOT NULL,
    investigator_verdict TEXT,
    investigator_reasoning TEXT,
    created_at TEXT NOT NULL,
    acted_on INTEGER NOT NULL DEFAULT 0,
    UNIQUE(wallet_address, market_id, detector_name, created_at)
);
CREATE INDEX idx_flags_created ON flags(created_at);
CREATE INDEX idx_flags_wallet ON flags(wallet_address);

-- Flag cost accounting — G7.
CREATE TABLE flag_costs (
    flag_id INTEGER PRIMARY KEY REFERENCES flags(id),
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cached_tokens INTEGER NOT NULL DEFAULT 0,
    cost_cents INTEGER NOT NULL,
    latency_ms INTEGER,
    created_at TEXT NOT NULL
);

-- Clock skew samples — G3.  Our wall clock vs WS server timestamp.
CREATE TABLE clock_skew_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    measured_at TEXT NOT NULL,
    skew_ms INTEGER NOT NULL,
    source TEXT NOT NULL
);

-- Known insiders seed list — §18 Q5.
CREATE TABLE known_insiders (
    label TEXT PRIMARY KEY,
    address TEXT,
    added_at TEXT NOT NULL,
    notes TEXT
);

-- Paper-trading runs
CREATE TABLE paper_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    config_json TEXT NOT NULL,
    starting_balance_cents INTEGER NOT NULL,
    current_balance_cents INTEGER NOT NULL,
    notes TEXT
);

-- Paper positions
CREATE TABLE paper_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES paper_runs(id),
    market_id TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK(outcome IN ('YES', 'NO')),
    size_shares INTEGER NOT NULL,
    avg_entry_price_cents INTEGER NOT NULL,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    realized_pnl_cents INTEGER,
    source_flag_id INTEGER REFERENCES flags(id),
    source_wallet TEXT,
    status TEXT NOT NULL CHECK(status IN ('OPEN', 'CLOSED', 'RESOLVED'))
);
CREATE INDEX idx_pos_run ON paper_positions(run_id);
CREATE INDEX idx_pos_status ON paper_positions(status);

-- Paper fills
CREATE TABLE paper_fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES paper_runs(id),
    position_id INTEGER NOT NULL REFERENCES paper_positions(id),
    side TEXT NOT NULL CHECK(side IN ('BUY', 'SELL')),
    size_shares INTEGER NOT NULL,
    fill_price_cents INTEGER NOT NULL,
    intended_price_cents INTEGER NOT NULL,
    slippage_cents INTEGER NOT NULL,
    latency_ms INTEGER NOT NULL,
    fee_cents INTEGER NOT NULL,
    timestamp TEXT NOT NULL
);
CREATE INDEX idx_fills_run ON paper_fills(run_id);

-- Metrics snapshots
CREATE TABLE metrics_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES paper_runs(id),
    as_of TEXT NOT NULL,
    metrics_json TEXT NOT NULL
);
CREATE INDEX idx_metrics_run ON metrics_snapshots(run_id);

-- Global key-value meta — timing base rates, etc.
CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
