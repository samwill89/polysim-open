-- External conversation-signal layer (signals package).
-- Adds:
--   conversation_snapshots : per (source, topic) windowed activity rows —
--                            the baseline history that attention z-scores
--                            and velocity ratios are computed against
--   market_signals         : per-market scored signal (unsigned conviction
--                            composite + confidence + components)
--   paper_positions.signal_composite / signal_multiplier : which positions
--                            were signal-influenced, for lift evaluation

CREATE TABLE IF NOT EXISTS conversation_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,                   -- snapshot time (UTC ISO)
    source TEXT NOT NULL,               -- 'reddit' | future providers
    topic TEXT NOT NULL,                -- normalized community key ('boxoffice')
    window_hours REAL NOT NULL,
    n_posts INTEGER NOT NULL DEFAULT 0,
    n_comments INTEGER NOT NULL DEFAULT 0,
    score_sum INTEGER NOT NULL DEFAULT 0,
    unique_authors INTEGER NOT NULL DEFAULT 0,
    posts_per_hour REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_conv_snap_topic
    ON conversation_snapshots(source, topic, ts DESC);

CREATE TABLE IF NOT EXISTS market_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,                   -- computed-at (UTC ISO)
    market_id TEXT NOT NULL,
    category TEXT,
    provider TEXT NOT NULL DEFAULT 'reddit',
    topics_json TEXT,                   -- JSON list of topics consulted
    matched_terms_json TEXT,            -- JSON list of terms that matched
    n_posts INTEGER NOT NULL DEFAULT 0,
    n_matched_posts INTEGER NOT NULL DEFAULT 0,
    attention_z REAL NOT NULL DEFAULT 0,
    velocity REAL NOT NULL DEFAULT 1,
    engagement REAL NOT NULL DEFAULT 0,
    breadth REAL NOT NULL DEFAULT 0,
    stance REAL NOT NULL DEFAULT 0,
    composite REAL NOT NULL DEFAULT 0.5,
    confidence REAL NOT NULL DEFAULT 0,
    components_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_market_signals_market
    ON market_signals(market_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_market_signals_ts
    ON market_signals(ts DESC);

ALTER TABLE paper_positions ADD COLUMN signal_composite REAL;
ALTER TABLE paper_positions ADD COLUMN signal_multiplier REAL;
