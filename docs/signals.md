# External conversation-signal layer (`polysim.signals`)

Public conversation activity → per-market conviction → bounded sizing /
gating for cohort copies, with pre-registered tournament variants so the
lift is *measured*, never assumed.

## Why this exists

The cohort-copy strategy treats every cohort trade identically: a
`CohortCopy` flag always carries composite 10.0, so sizing has no
market-specific conviction input at all (Kelly variants were effectively
sizing on a constant). Meanwhile the equity lane already validated that
**mention-volume anomaly (attention) is the predictive part of social
data**, with polarity a weak secondary. This package generalizes that
finding to the Polymarket lane: markets whose underlying event the public
is actively talking about (r/boxoffice for opening-weekend markets,
r/LocalLLaMA for AI-launch markets, …) get more conviction; markets with
a confidently-dead conversation can be skipped.

## Direction policy (important)

The composite is **unsigned conviction in [0, 1]**. Direction always
comes from the cohort wallet. We deliberately do NOT map "positive
chatter" to YES — that requires reading the market question's semantics
(is it "Will X gross $100M?" or "Will X flop?"), which is LLM territory.
The extracted `stance ∈ [-1, 1]` is stored on every signal row and passed
to the Tier-2 investigator prompt (`InvestigatorContext.external_signal_note`),
where a model that *can* read the question may use it. Deterministic code
never signs the composite with it.

## Architecture

```
providers.py   RedditPublicProvider   reddit.com/r/<sub>/new.json — free,
                                      credential-less; any failure → None
               FixtureProvider        JSON files on disk (tests/backtests)
               StaticProvider         in-memory (unit tests)
linker.py      market → topics (category → subreddits) + search terms
               extracted from the market question (deterministic)
extract.py     pure features: windowed activity, attention z-score vs own
               history, velocity ratio, engagement, breadth, lexicon stance
scoring.py     composite = 0.50·σ(z) + 0.25·σ(log2 velocity)
                          + 0.15·engagement + 0.10·breadth
               confidence = post coverage × baseline coverage
                            (capped at 0.5 on community-tide fallback)
               conviction_multiplier: [0,1] → [min_mult, max_mult],
               pulled toward 1.0 by low confidence; None → exactly 1.0
service.py     fetch → snapshot (conversation_snapshots) → score
               (market_signals) → serve; SignalSnapshotLoop for live
evaluation.py  realized-only lift measurement (see below)
```

Tables (migration `0009_market_signals.sql`):

* `conversation_snapshots` — per (source, topic) windowed activity; this
  is the baseline history z-scores and velocity rest on. Signals stay at
  z=0 / velocity 1x (≈ neutral) until ≥ `min_baseline_snapshots` history
  rows exist — the layer *earns* its conviction.
* `market_signals` — per-market scored rows with full components JSON
  (auditable/reproducible per spec §14 #5).
* `paper_positions.signal_composite` / `signal_multiplier` — which
  positions the executor actually adjusted, for A/B evaluation.

## How it affects trading (all off by default)

`RiskProfile.signal_policy`:

* `off` (default, all built-ins + historical snapshots) — zero change.
* `size` — position size × `conviction_multiplier` ∈
  [`signal_size_min_mult`, `signal_size_max_mult`] (default 0.5x–1.5x).
* `gate_and_size` — additionally skip a copy when a *fresh, confident*
  signal says the conversation is dead
  (`composite < signal_gate_min_composite` with
  `confidence ≥ signal_gate_min_confidence`).

Degradation contract: missing signal row, stale row
(> `signal_max_age_hours`), low confidence, provider failure, or any
exception → multiplier exactly 1.0 and gate open. The layer can only ever
be a no-op, never a source of unintended vetoes.

Two registered research variants isolate the effect
(`tournament/variants.py`): `signal_sized` and `signal_gated` are
baseline-identical except for `signal_policy`. They are retained for
reproducibility but paused in strategy set v2 because the deployed database
had zero `conversation_snapshots` and zero `market_signals` on 2026-07-10.
They must not be activated until a provider produces a stable pre-trade
history and a clean forward A/B can begin.

## Running it

```bash
# one-shot (works with signals.enabled: false; needs egress to reddit.com)
uv run polysim signals snapshot

# offline / deterministic (fixtures = JSON lists of posts per topic)
uv run polysim signals snapshot --provider fixture --fixtures-dir tests/fixtures/signals

# inspect
uv run polysim signals show
uv run polysim signals eval        # measured lift, realized-only
```

Live loop: set `signals.enabled: true` in config.yml AND
`LiveConfig.enable_signals=True` (the live command's flag). Cadence
`signals.snapshot_interval_s` (default 1h ≈ 24 baseline points/day/topic).

**Fly caveat:** reddit.com's public JSON is known to 403 from datacenter
IPs (documented in `equity/sentiment.py`). On Fly the provider will
likely degrade to no-ops — verify with `polysim signals show` after a few
hours before believing any live claim. Local/residential runs work. An
aggregator-style provider (the equity lane's ApeWisdom pattern) is the
natural fallback; implement `ConversationProvider` and register it in
`service.provider_from_config`.

## Measuring the edge (never assume it)

* `polysim signals eval` / `signals/evaluation.py`:
  * `signal_bucket_outcomes` — resolved markets bucketed by their last
    pre-resolution composite (low/mid/high): positions, realized P&L,
    win rate per bucket. If high-conviction buckets don't beat low, the
    signal earns no size.
  * `signal_sizing_summary` — realized P&L of signal-adjusted vs
    untouched positions (the tournament A/B, readable any time).
  * All realized-only: MTM and open exposure never enter these numbers.
* Tournament: after at least 30 resolved positions each, compare
  `signal_sized` / `signal_gated` vs `baseline` in
  `polysim tournament status`. Open marks do not satisfy this gate.

## Extending

* New provider: implement `ConversationProvider.fetch_posts` (return
  `None` on failure, `[]` on genuinely-empty) and wire it in
  `provider_from_config`.
* New category mapping: `signals.category_subreddits` in config.yml
  overrides the `intel_sources`-derived map.
* Stance→direction: build on `stance` + the investigator prompt note;
  do NOT sign the deterministic composite.
