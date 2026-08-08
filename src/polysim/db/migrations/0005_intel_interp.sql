-- Intel interpretations — LLM-parsed sentiment from each intel_message.
-- One row per (message, model, prompt_version). Multiple interpretations
-- allowed so we can re-run as the prompt evolves without losing history.
CREATE TABLE IF NOT EXISTS intel_interpretations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    intel_message_id INTEGER NOT NULL REFERENCES intel_messages(id),
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    -- Structured output
    is_market_relevant INTEGER NOT NULL,   -- 0/1
    sentiment TEXT,                        -- 'bullish' | 'bearish' | 'neutral' | 'mixed' | null
    direction TEXT,                        -- 'YES' | 'NO' | null
    conviction REAL,                       -- 0..1
    market_hint TEXT,                      -- human phrase, e.g. "US x Iran ceasefire by Apr 15"
    matched_market_id TEXT,                -- fuzzy-matched to markets.id, nullable
    match_score REAL,                      -- 0..1 similarity
    tags_json TEXT NOT NULL DEFAULT '[]',  -- ["insider", "coordination", "thread"...]
    summary TEXT,                          -- 1-sentence paraphrase
    cost_cents INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_interp_msg ON intel_interpretations(intel_message_id);
CREATE INDEX IF NOT EXISTS idx_interp_market ON intel_interpretations(matched_market_id);
CREATE INDEX IF NOT EXISTS idx_interp_relevant ON intel_interpretations(is_market_relevant, created_at DESC);
