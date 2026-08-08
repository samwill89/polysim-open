-- Intel messages — Tier 3 Telegram-channel scraper.
-- Each row is one message from a watched public channel, with any
-- extracted wallets / market slugs / categories normalized out.

CREATE TABLE IF NOT EXISTS intel_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,                 -- channel username, e.g. 'spaceinsights'
    external_id TEXT NOT NULL,            -- Telegram message id (str, unique per source)
    posted_at TEXT NOT NULL,              -- ISO UTC
    author TEXT,                          -- sender name / username, nullable
    text TEXT NOT NULL,
    wallets_json TEXT NOT NULL DEFAULT '[]',
    market_slugs_json TEXT NOT NULL DEFAULT '[]',
    categories_json TEXT NOT NULL DEFAULT '[]',
    raw_json TEXT,                        -- original message envelope (for re-processing)
    ingested_at TEXT NOT NULL,
    UNIQUE(source, external_id)
);
CREATE INDEX IF NOT EXISTS idx_intel_posted ON intel_messages(posted_at);
CREATE INDEX IF NOT EXISTS idx_intel_source ON intel_messages(source, posted_at DESC);

-- known_insiders.source: where did we learn this wallet from?
-- 'operator' (manual config.yml), 'intel:spaceinsights', etc.
ALTER TABLE known_insiders ADD COLUMN source TEXT NOT NULL DEFAULT 'operator';
ALTER TABLE known_insiders ADD COLUMN source_message_id INTEGER
    REFERENCES intel_messages(id);
CREATE INDEX IF NOT EXISTS idx_known_insiders_source ON known_insiders(source);
