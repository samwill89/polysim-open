-- Equity sentiment track (parallel to the Polymarket copy-trade track).
-- Adds:
--   equity_quotes    : daily OHLC cache (Yahoo), cents, per (ticker, date)
--   equity_sentiment : raw per-source mention/stance snapshots (ApeWisdom, StockTwits)
--   equity_signals   : daily aggregated composite score per (ticker, date)
--   equity_positions : run-scoped, ticker-based positions with exit_reason
-- Variant runs reuse paper_runs with tag = 'equity_v1'.

CREATE TABLE IF NOT EXISTS equity_quotes (
    ticker TEXT NOT NULL,
    date TEXT NOT NULL,                 -- YYYY-MM-DD (UTC)
    open_cents INTEGER NOT NULL,
    high_cents INTEGER NOT NULL,
    low_cents INTEGER NOT NULL,
    close_cents INTEGER NOT NULL,
    volume INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'yahoo',
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (ticker, date)
);
CREATE INDEX IF NOT EXISTS idx_equity_quotes_ticker ON equity_quotes(ticker, date DESC);

CREATE TABLE IF NOT EXISTS equity_sentiment (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,                   -- ingest timestamp (UTC ISO)
    ticker TEXT NOT NULL,
    source TEXT NOT NULL,              -- 'apewisdom' | 'stocktwits' | 'x' | ...
    mentions INTEGER NOT NULL DEFAULT 0,
    mention_change_pct REAL,           -- 24h change where the source provides it
    upvotes INTEGER NOT NULL DEFAULT 0,
    bull_count INTEGER NOT NULL DEFAULT 0,
    bear_count INTEGER NOT NULL DEFAULT 0,
    rank INTEGER,
    raw_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_equity_sent_ticker ON equity_sentiment(ticker, ts DESC);
CREATE INDEX IF NOT EXISTS idx_equity_sent_ts ON equity_sentiment(ts DESC);

CREATE TABLE IF NOT EXISTS equity_signals (
    date TEXT NOT NULL,                 -- trading day the signal is for
    ticker TEXT NOT NULL,
    composite REAL NOT NULL DEFAULT 0,  -- final score (attention-led)
    z_attn REAL NOT NULL DEFAULT 0,     -- mention-volume z-score vs own history
    stance REAL NOT NULL DEFAULT 0,     -- credibility-weighted polarity [-1,1]
    novelty REAL NOT NULL DEFAULT 0,
    disagreement REAL NOT NULL DEFAULT 0,
    mentions INTEGER NOT NULL DEFAULT 0,
    components_json TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (date, ticker)
);
CREATE INDEX IF NOT EXISTS idx_equity_signals_date ON equity_signals(date DESC);

CREATE TABLE IF NOT EXISTS equity_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES paper_runs(id),
    ticker TEXT NOT NULL,
    shares REAL NOT NULL,
    avg_entry_price_cents INTEGER NOT NULL,
    high_water_cents INTEGER NOT NULL DEFAULT 0,   -- highest close since entry (trailing stops)
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    exit_price_cents INTEGER,
    realized_pnl_cents INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'OPEN',            -- OPEN | CLOSED
    exit_reason TEXT,
    source_signal REAL
);
CREATE INDEX IF NOT EXISTS idx_equity_pos_run ON equity_positions(run_id, status);
CREATE INDEX IF NOT EXISTS idx_equity_pos_ticker ON equity_positions(ticker, status);

-- Uninvested cash per equity run (total equity = cash + marked positions).
CREATE TABLE IF NOT EXISTS equity_run_state (
    run_id INTEGER PRIMARY KEY REFERENCES paper_runs(id),
    cash_cents INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
