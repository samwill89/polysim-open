-- Entity extraction added to intel_interpretations (v2-entities prompt).
-- Each row's `entities_json` is a JSON array of short proper-noun strings
-- the LLM extracted, used by the entity-aware matcher to shortlist
-- candidate markets.
ALTER TABLE intel_interpretations
    ADD COLUMN entities_json TEXT NOT NULL DEFAULT '[]';
