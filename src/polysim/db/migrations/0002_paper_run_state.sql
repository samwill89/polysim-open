-- Phase 4 additions: pause state + orderbook snapshots (G8).
-- Idempotent — safe to re-run.

-- paper_runs.paused_at: kill-switch trips set this; manual resume clears it.
ALTER TABLE paper_runs ADD COLUMN paused_at TEXT;
ALTER TABLE paper_runs ADD COLUMN pause_reason TEXT;

-- Orderbook snapshots — drives realistic fill modelling (spec §9, G8).
-- Each row is the book for one (market, outcome) token at a point in time.
-- asks/bids JSON: [{"price_cents": int, "size_shares": int}, ...] sorted
-- ascending (asks) / descending (bids).
CREATE TABLE IF NOT EXISTS orderbook_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT NOT NULL REFERENCES markets(id),
    outcome TEXT NOT NULL CHECK(outcome IN ('YES', 'NO')),
    timestamp TEXT NOT NULL,
    asks_json TEXT NOT NULL,
    bids_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_book_market_time
    ON orderbook_snapshots(market_id, outcome, timestamp DESC);
