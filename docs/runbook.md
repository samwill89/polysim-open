## Empirical-priors addendum (Apr 2026 + Apr 24 supplement)

**The 30-day operator flow:**

```bash
# 1. Init (idempotent — applies migration 0007)
polysim init

# 2. Pull / refresh poly_data + run discovery + freeze cohort
polysim discovery run --experiment-id experiment_001
polysim discovery show-cohort --niche aec
polysim discovery coverage

# 3. Start the live orchestrator (paper-only, autonomous, 24/7)
#    Settlement sweep + bid-price MTM are now active automatically.
polysim live --profiles systematic,medium,degen --tag prod-live

# 4. (Optional, daily) review intel sentiment
polysim intel sync-all
polysim intel coverage

# 5. End-of-experiment hypothesis report (lands when E6/E7/E8 ship)
#    polysim experiment report --experiment-id experiment_001
```

**Current decision path:** the deployed cohort-copy tournament opens paper
positions from `CohortCopy` flags subject to each run's risk, liquidity,
copyability, fill-depth, concentration, evidence, shared-subject correlation,
and executable-edge rules. Catalyst-sensitive markets fail closed unless a
fresh claim-specific scan and analyst probability pass the v3 thresholds.
External conversation signals remain off until they have stable pre-trade
history; attention is never treated as confirmation.

**Live-trading (real money) is still architecturally prohibited.** Every
spec doc + the addendum's §10 + multiple CI greps enforce this. If
24-hour autonomous *paper* observation and cohort-copy simulation are active
through `polysim live`. Real-money mode is out of scope until the paper
experiment shows measurable edge.

---

# Operator runbook

Living document for day-to-day PolySim operation. Mirrors `polysim-spec.md` §12 phase structure.

---

## Phase 0 — Scaffold + guardrails (today)

```bash
python -m pip install --user uv
uv python install 3.12
uv sync --extra dev

cp .env.example .env                # then edit: ANTHROPIC_API_KEY, ALCHEMY_API_KEY, TELEGRAM_*
cp config.example.yml config.yml
uv run polysim init
uv run polysim doctor               # schema, config, secrets, trade flow
```

Health checks (run any time):

```bash
uv run ruff check .
uv run mypy src/polysim
uv run pytest
uv run python scripts/ci_safety.py  # forbids place_order / sign_order in src/
```

---

## Phase 1+ — Live ingest

```bash
uv run polysim ingest start         # one terminal; Ctrl+C to stop
uv run polysim ingest backfill --days 30   # one-shot historical pull
uv run polysim status               # quick live counters
```

---

## Phase 2+ — Scoring / flag inspection

```bash
uv run polysim profile rebuild      # force-recompute all wallet profiles
uv run polysim flags list --since 24h
uv run polysim flags show <id>
```

---

## Phase 3+ — Investigator (manual invocation)

```bash
uv run polysim investigate <flag_id>
# Or bulk lift measurement:
uv run python scripts/measure_investigator_lift.py --since 7d --out reports/lift.md
```

---

## Phase 4+ — Paper runs

```bash
uv run polysim run start --config config.yml
uv run polysim run list
uv run polysim run status <run_id>
uv run polysim run stop <run_id>
```

---

## Phase 5+ — Reports

```bash
# Full Markdown report with null + favorite baselines + calibration plot
uv run polysim report --run <id> --out reports/run-<id>.md \
    --calibration-png reports/calibration-<id>.png

# Calibrate fill-model against last 30d of fills
uv run polysim calibrate fill-model --days 30
```

---

## Phase 6 — Live paper mode

### Pre-flight (30-day run)

1. **Keys loaded**: `ANTHROPIC_API_KEY`, `ALCHEMY_API_KEY`; Telegram optional.
2. **Config reviewed**: `config.yml` — confirm `run.starting_balance_cents: 1_000_000` (§18 Q1), `investigator.max_calls_per_day: 100`, kill-switch thresholds in `bankroll`.
3. **DB clean**: `uv run polysim doctor` — all green.
4. **Disk space**: SQLite grows at ~1 MB/day from trade stream; provision ≥ 100 MB for the 30-day window.
5. **Telegram bot**: create via @BotFather, paste token into `.env`. Send `/start` to verify chat_id. Test alerts with:
   ```bash
   uv run python -c "import asyncio; from polysim.reporter.telegram import *; sink = make_sink_from_config(enabled=True, bot_token='...', chat_id='...'); asyncio.run(sink.send_daily_summary(DailySummaryAlert(as_of_iso='2026-04-20', runs=[], top_category=None, worst_category=None, llm_calls_today=0, llm_cost_cents_today=0, kill_switches_nominal=True)))"
   ```

### Starting the 30-day run

Open three terminals:

```bash
# 1. Ingest firehose
uv run polysim ingest start

# 2. Paper run + processing (long-running — Phase 6 will wire a single-
#    process daemon; today run periodically via cron or a loop)
uv run polysim run start --config config.yml

# 3. Live dashboard (Ctrl+C to exit)
uv run polysim dashboard --run <id_from_previous_command>
```

Daily summary fires at 09:00 local (configurable via `logging.scheduler_hour`).

### Health monitoring during the run

- Uptime watchdog: `utils.watchdog.UptimeWatchdog` fires a Telegram alert when no trade lands for ≥ 5 minutes.
- Kill switches: drawdown > 20% of starting balance OR > 100 flags/hour AUTO-PAUSES the run. `polysim run status <id>` shows the pause reason; resume via DB update (`dao.resume_paper_run(run_id)`).
- `polysim doctor` nightly as a cron check.

### Exit criteria (§12 Phase 6)

- 30 consecutive days live
- No crash on the run process
- ≥ 95 % uptime (measured via gaps in trade-ingest timestamps)
- Final report generated (`polysim report --run <id> --out reports/final.md`)

### Post-run retro (task 6.8)

1. **Run the final report**:
   ```bash
   uv run polysim report --run <id> --out reports/final-<id>.md \
       --calibration-png reports/final-calibration-<id>.png
   ```
2. **Null & favorite baselines**: already generated by `polysim report`; check p-values. If either is > 0.05 the 30-day run did not demonstrate edge (§10 credibility gate).
3. **Weight tuning**: if per-detector Shapley shows one dominant contributor, consider re-balancing `scoring.weights` in `config.yml`. DO NOT tune on the same window — hold back the next 30-day replay as a cross-validation set.
4. **Promote confirmed wallets**: wallets with ≥ 3 INFORMED + profitable outcomes should be pinned into `config.known_insiders[]`. Update addresses in the YAML and re-run the next window.
5. **Demote pop_culture / box_office** if their 30-day category P&L is negative two runs in a row (§9 risk register).
6. **Archive reports**: commit `reports/final-*.md` + `reports/calibration-*.png` to a local git repo (not this one — spec §2 forbids multi-tenant hosting).

---

## Commands quick reference

```
polysim init                            # schema + config
polysim doctor                          # health check
polysim status                          # live counters
polysim ingest start / backfill --days N
polysim profile rebuild                 # wallet profiles
polysim profile list / show / create / edit   # risk profiles (addendum §4.3)
polysim flags list / show
polysim investigate <flag_id>
polysim evidence scan <market_id>
polysim evidence show <market_id>
polysim evidence set-probability <market_id> --p-yes 0.62 --confidence 0.75 --summary "..."
polysim run start --profile <name> [--tag <t>]
polysim run start-all --tag <t> [--profiles systematic,medium,degen]
polysim run stop / status / list / resume
polysim run compare --tag <t>           # side-by-side metrics
polysim report --run N [--out path]
polysim report --tag <t> [--out path]   # comparative Markdown w/ bootstrap (addendum §7)
polysim calibrate fill-model --days N
polysim dashboard [--run N] [--split | --compare <tag>]
polysim replay --from DATE --to DATE --speed 100x
polysim live --profiles systematic --tag experiment_002 --evidence  # one-command orchestrator
polysim web --port 8765                              # read-only browser dashboard
polysim replay --from 2026-03-20 --to 2026-04-19 --profile systematic
```

---

## Phase 4.5 — Dual-mode risk profiles (addendum)

The operator can run multiple paper runs with different risk profiles against
the same flag stream:

```bash
# Inspect the built-ins
polysim profile list
polysim profile show degen

# Launch all three against one flag stream
polysim run start-all --tag dual-mode-exp-01 --balance 1000000

# Watch the three side-by-side
polysim dashboard --compare dual-mode-exp-01

# 30 days later — comparative report w/ 1000-iter bootstrap
polysim report --tag dual-mode-exp-01 --out reports/dual-mode-01.md
```

### First-experiment protocol (addendum §9)

1. **Day 0:** launch all three runs at $10,000 each with `--tag
   dual-mode-experiment-01`. Record config snapshots. Do NOT touch detectors
   or weights during the experiment — changing the signal mid-run invalidates
   the comparison.
2. **Days 1–30:** check dashboard daily (`--compare <tag>`). Note any profile
   that pauses (drawdown / daily-loss limit). Do NOT `run resume` during the
   window — a paused degen run is itself a data point.
3. **Day 30:** `polysim report --tag <tag> --out reports/final-exp01.md`.
   Answer:
   - Best return? Best Sharpe?
   - If degen made money, from one lucky flag or distributed wins?
   - Which profile's *process* do you want to live with?
   - Execution drag per profile (thin markets = more drag)?

### Addendum §11 warning

At some point during the 30-day window, the degen run will probably be up 300%+
while systematic grinds along at +5%. The instinct to "switch to degen with
real money" is exactly the survivorship-bias trap this experiment is designed
to reveal. The relevant question after 30 days is NOT "which profile is up
more right now?" — it's "given the distribution of 30-day outcomes for each
profile, which one do you want to run for five years?" The comparative report's
bootstrap section prints that distribution explicitly.

---

## `polysim live` — one-command orchestrator

```bash
polysim live --profiles systematic,medium,degen --tag apr-exp
```

Wires: ingest → scorer → dispatcher → N profile-aware executors → resolution
closer → watchdog → daily Telegram summary → inbound Telegram bot. Paper-only
(§16) + terminal/Telegram only (§2). Ctrl+C stops cleanly.

### Web dashboard (companion to TUI + Telegram)

```bash
polysim web --port 8765
# Open http://127.0.0.1:8765/ in any browser.
```

Read-only single-page UI mirroring the demo's 5 surfaces — ingest stream,
TUI, flag detail, Telegram chat, runs list. Refreshes every 2s. Spec §2's
no-web-UI ban is **deliberately overridden** here per operator request;
the §16 paper-only invariant remains intact (no endpoint mutates state).

Bind defaults to `127.0.0.1`; use `--host 0.0.0.0` only on a trusted LAN
(no auth — single-operator).

### Historical replay

```bash
# Drives the same scoring pipeline as live ingest over a stored window
polysim replay --from 2026-03-20 --to 2026-04-19 --profile systematic
# --no-run skips the paper-run pass and just refreshes flags/profiles.
```

The replayer is offline; `--speed` is accepted for CLI compat but ignored.

### Timing baselines

```bash
# Compute per-category late-activity base rates after Phase 1 backfill,
# then re-run anything that uses TimingDetector.
.venv/Scripts/python.exe scripts/build_timing_baselines.py --window-hours 1
```

Until run, TimingDetector falls back to the 0.20 default for every category.

### Stop-loss sweep

The `live` orchestrator runs a periodic mark-to-market sweep that closes
any open position whose mark dropped by `stop_loss_pct` below entry,
when the active profile has `stop_loss_enabled: true`. Disable with
`--no-ingest` style flags via `LiveConfig.enable_stop_loss_sweep=False`
(programmatic). Mark price preference: latest orderbook mid, falling back
to the most recent (market, outcome) trade price.

Protocol fees are read from each market's persisted `feeSchedule` when
available and are charged on paper entries and exits. Category rates are only
a fallback. See `docs/risk-intelligence.md` for the complete v3 gate order.

### Inbound Telegram bot

From the authorized chat only:

```
/status              system stats
/flags [since=24h] [limit=10]
/runs                all paper runs
/run <id>            one run's detail
/compare <tag>       all runs sharing a tag
/report <run_id>     headline metrics
/help                this list
```

Read-only — no command opens positions, edits profiles, or touches keys.
Requests from any chat other than `TELEGRAM_CHAT_ID` are rejected.

---

## Safety re-statement (spec §16)

**This system never places a real trade.** The operator may act on information it surfaces — outside this software, in their own head and hands. That air gap is intentional and non-negotiable.
