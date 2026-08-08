-- Repair paper bankrolls affected by the pre-v13 cohort mirror-exit bug.
--
-- Bug signature: status=CLOSED with realized P&L but no SELL fill. The old
-- _mirror_sell_close path recorded (exit-entry)*shares on the position yet
-- failed to return exit*shares to paper_runs.current_balance_cents.
--
-- The reconciliation table is durable evidence and makes this script safe if
-- the SQL commits but migration bookkeeping is interrupted and retried.

BEGIN IMMEDIATE;

CREATE TABLE IF NOT EXISTS paper_balance_reconciliations (
    position_id INTEGER PRIMARY KEY REFERENCES paper_positions(id),
    run_id INTEGER NOT NULL REFERENCES paper_runs(id),
    reason TEXT NOT NULL,
    inferred_exit_price_cents INTEGER NOT NULL,
    credit_cents INTEGER NOT NULL,
    discovered_at TEXT NOT NULL,
    credited_at TEXT
);

INSERT OR IGNORE INTO paper_balance_reconciliations(
    position_id, run_id, reason, inferred_exit_price_cents,
    credit_cents, discovered_at, credited_at
)
SELECT
    p.id,
    p.run_id,
    'mirror_sell_missing_credit',
    CAST(
        (p.size_shares * p.avg_entry_price_cents + p.realized_pnl_cents)
        / p.size_shares AS INTEGER
    ),
    p.size_shares * p.avg_entry_price_cents + p.realized_pnl_cents,
    CURRENT_TIMESTAMP,
    NULL
FROM paper_positions p
JOIN paper_runs pr ON pr.id = p.run_id
JOIN flags f ON f.id = p.source_flag_id
WHERE p.status = 'CLOSED'
  AND pr.tag = 'tournament_v1'
  AND f.detector_name = 'CohortCopy'
  AND p.realized_pnl_cents IS NOT NULL
  AND p.size_shares > 0
  AND p.size_shares * p.avg_entry_price_cents + p.realized_pnl_cents >= 0
  AND p.size_shares * p.avg_entry_price_cents + p.realized_pnl_cents
      <= p.size_shares * 100
  AND NOT EXISTS (
      SELECT 1 FROM paper_fills f
      WHERE f.position_id = p.id AND f.side = 'SELL'
  )
  AND EXISTS (
      SELECT 1 FROM trades t
      WHERE LOWER(t.wallet_address) = LOWER(p.source_wallet)
        AND t.market_id = p.market_id
        AND t.outcome = p.outcome
        AND t.side = 'SELL'
        AND t.price_cents = CAST(
            (p.size_shares * p.avg_entry_price_cents + p.realized_pnl_cents)
            / p.size_shares AS INTEGER
        )
        AND t.timestamp >= p.opened_at
        AND t.timestamp <= p.closed_at
  );

UPDATE paper_runs
SET current_balance_cents = current_balance_cents + COALESCE((
    SELECT SUM(r.credit_cents)
    FROM paper_balance_reconciliations r
    WHERE r.run_id = paper_runs.id AND r.credited_at IS NULL
), 0)
WHERE EXISTS (
    SELECT 1 FROM paper_balance_reconciliations r
    WHERE r.run_id = paper_runs.id AND r.credited_at IS NULL
);

INSERT INTO paper_fills(
    run_id, position_id, side, size_shares, fill_price_cents,
    intended_price_cents, slippage_cents, latency_ms, fee_cents, timestamp
)
SELECT
    r.run_id,
    r.position_id,
    'SELL',
    p.size_shares,
    r.inferred_exit_price_cents,
    r.inferred_exit_price_cents,
    0,
    0,
    0,
    COALESCE(p.closed_at, r.discovered_at)
FROM paper_balance_reconciliations r
JOIN paper_positions p ON p.id = r.position_id
WHERE r.credited_at IS NULL;

UPDATE paper_balance_reconciliations
SET credited_at = CURRENT_TIMESTAMP
WHERE credited_at IS NULL;

COMMIT;
