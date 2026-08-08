BEGIN IMMEDIATE;

CREATE TABLE IF NOT EXISTS market_evidence_assessments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT NOT NULL REFERENCES markets(id),
    subject_key TEXT,
    assessed_at TEXT NOT NULL,
    provider TEXT NOT NULL,
    assessment_kind TEXT NOT NULL,
    catalyst_sensitive INTEGER NOT NULL,
    status TEXT NOT NULL,
    source_count INTEGER NOT NULL DEFAULT 0,
    relevant_source_count INTEGER NOT NULL DEFAULT 0,
    independent_domain_count INTEGER NOT NULL DEFAULT 0,
    primary_source_count INTEGER NOT NULL DEFAULT 0,
    high_quality_source_count INTEGER NOT NULL DEFAULT 0,
    rumor_article_count INTEGER NOT NULL DEFAULT 0,
    confirmation_article_count INTEGER NOT NULL DEFAULT 0,
    source_quality_score REAL NOT NULL DEFAULT 0,
    rumor_risk_score REAL NOT NULL DEFAULT 0,
    information_asymmetry_score REAL NOT NULL DEFAULT 0,
    fair_probability_yes REAL,
    probability_confidence REAL NOT NULL DEFAULT 0,
    summary TEXT NOT NULL,
    sources_json TEXT NOT NULL DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS idx_evidence_market_time
    ON market_evidence_assessments(market_id, assessed_at DESC);
CREATE INDEX IF NOT EXISTS idx_evidence_subject_time
    ON market_evidence_assessments(subject_key, assessed_at DESC);

CREATE TABLE IF NOT EXISTS trade_risk_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES paper_runs(id),
    flag_id INTEGER REFERENCES flags(id),
    market_id TEXT NOT NULL REFERENCES markets(id),
    position_id INTEGER REFERENCES paper_positions(id),
    assessment_id INTEGER REFERENCES market_evidence_assessments(id),
    policy TEXT NOT NULL,
    subject_key TEXT,
    catalyst_sensitive INTEGER NOT NULL,
    evidence_status TEXT,
    correlated_positions INTEGER NOT NULL DEFAULT 0,
    subject_exposure_cents INTEGER NOT NULL DEFAULT 0,
    entry_price_cents INTEGER NOT NULL,
    fee_cents INTEGER NOT NULL DEFAULT 0,
    spread_cents INTEGER,
    fair_value_cents REAL,
    uncertainty_penalty_cents REAL NOT NULL DEFAULT 0,
    edge_after_cost_cents REAL,
    passed INTEGER NOT NULL,
    gate_name TEXT NOT NULL,
    reason TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_risk_decisions_run_time
    ON trade_risk_decisions(run_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_risk_decisions_market_time
    ON trade_risk_decisions(market_id, created_at DESC);

COMMIT;
