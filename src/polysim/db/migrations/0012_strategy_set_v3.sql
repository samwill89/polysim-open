BEGIN IMMEDIATE;

-- The v3 live run starts with evidence, correlation, fee, and edge gates.
-- Keep the old run intact for comparison, but do not present it as active.
UPDATE paper_runs
SET paused_at = COALESCE(paused_at, strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    pause_reason = 'strategy_set_v3: superseded by verified-edge controls'
WHERE tag = 'experiment_001'
  AND name = 'experiment_001-systematic'
  AND ended_at IS NULL
  AND paused_at IS NULL;

COMMIT;
