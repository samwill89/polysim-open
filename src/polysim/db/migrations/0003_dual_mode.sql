-- Dual-mode addendum §4.2, §4.4 — per-run risk profile snapshot + tag.
-- profile_name: which RiskProfile was selected at run start.
-- profile_snapshot_json: full RiskProfile dump at run start, so mid-run
--   profile file edits do not retroactively change historical runs.
-- tag: optional grouping key for comparative reports (`--tag <tag>`).

ALTER TABLE paper_runs ADD COLUMN profile_name TEXT NOT NULL DEFAULT 'systematic';
ALTER TABLE paper_runs ADD COLUMN profile_snapshot_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE paper_runs ADD COLUMN tag TEXT;
CREATE INDEX IF NOT EXISTS idx_paper_runs_tag ON paper_runs(tag);
