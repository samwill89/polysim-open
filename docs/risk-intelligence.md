# Risk intelligence and strategy set v3

Strategy set v3 is a prospective paper-trading control. It does not rewrite
old positions or claim that historical marks would have been executable.

## What changed

Every v3 candidate still passes the existing detector, liquidity, sizing,
depth, and portfolio controls. The executor then applies these additional
checks at the simulated fill:

1. Extract a shared named subject from the market question.
2. Reject a second open position on that subject or exposure above the profile
   cap, even when the positions use different market IDs.
3. For catalyst-sensitive questions, fetch a fresh public-news scan from
   GDELT using both the subject and event-family terms, with Google News RSS
   as a rate-limit and outage fallback.
4. Count only sources whose title is relevant to the actual catalyst. An
   unrelated Reuters or AP story about the same person is not corroboration.
5. Score source tier, independent-domain breadth, rumor language, explicit
   confirmation language, and information asymmetry.
6. Require an analyst `P(YES)` and confidence for sensitive markets. News
   volume, social popularity, or wallet conviction cannot supply this value.
7. Calculate edge from fair value minus the actual simulated fill, market fee,
   and a confidence-based uncertainty penalty. Require at least the profile's
   configured post-cost edge.

The decision and its evidence snapshot are persisted in
`trade_risk_decisions` and `market_evidence_assessments`. The web dashboard
shows recent passes, blocks, evidence status, and catalyst-relevant source
counts.

## Operator flow

```bash
polysim evidence scan <market_id> --db polysim.db --config config.yml
polysim evidence show <market_id> --db polysim.db
polysim evidence set-probability <market_id> \
  --p-yes 0.62 --confidence 0.75 \
  --summary "Base rate plus corroborated event evidence" \
  --db polysim.db
```

`set-probability` is deliberately explicit. If the source scan is stale,
rumor-heavy, low quality, or unrelated to the catalyst, the probability does
not override that failure. A sensitive market with no probability is blocked.

## Fee accounting

Paper taker fees use `feeSchedule.rate` and `feeSchedule.exponent` from the
persisted market metadata when present. The calculation is:

```text
fee = shares * rate * (price * (1 - price)) ** exponent
```

Fees are recorded on entry, cohort-mirrored exits, and stop-loss exits. The
category table is used only when a fee-enabled market lacks schedule metadata.
Because the paper ledger stores whole cents while venue fees have finer
precision, every positive sub-cent fee is rounded up to one paper cent.

## Active lanes

The live v3 tournament creates clean runs for:

- `baseline_verified`
- `tight_stop_verified`
- `liquid_verified`
- `sports_politics_verified`

The prior compact v2 lanes and unfinished `experiment_001` runs are paused,
not deleted. Promotion still requires at least 30 resolved positions per lane
and superiority after costs; open marks remain diagnostic only.

## References

- Polymarket fee formula and category schedule:
  https://docs.polymarket.com/trading/fees
- Polymarket requirement to use each market's `feeSchedule`:
  https://docs.polymarket.com/changelog
- GDELT DOC 2.0 API:
  https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/
