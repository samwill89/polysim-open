BEGIN IMMEDIATE;

-- Old experiment_001 runs are retained as historical evidence, but none are
-- part of the v3 dispatcher. Mark every unfinished lane as retired so the
-- dashboard cannot imply that stale controls are still active.
UPDATE paper_runs
SET paused_at = COALESCE(paused_at, strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    pause_reason = 'strategy_set_v3: legacy experiment retired'
WHERE tag = 'experiment_001'
  AND ended_at IS NULL;

COMMIT;
