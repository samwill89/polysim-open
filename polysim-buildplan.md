# PolySim — Build Plan

**Version:** 0.1  **Companion to:** `polysim-spec.md` (v0.1) & `polysim-demo.html` (UI targets)
**Owner:** the operator  **Planned:** 2026-04-19

This plan expands spec §12 into task-level work, pins spec gaps to owners & phases, and sets demoable checkpoints. Estimation units: **S** < ½ day · **M** ½–1 day · **L** 1–2 days · **XL** 3+ days.

---

## 0. Phase 0 — Cross-cutting setup

Finishes the §17 skeleton and locks non-negotiable guardrails. Nothing in later phases starts until all of §0 is green.

| # | Task | Files | Size |
|---|---|---|---|
| 0.1 | `uv init`, pin Python 3.12, commit `pyproject.toml` with all §4 deps | `pyproject.toml` | S |
| 0.2 | Ruff + `mypy --strict` + pytest + hypothesis; pre-commit hooks | `.pre-commit-config.yaml`, `pyproject.toml` | S |
| 0.3 | Directory skeleton per §5; every module stubbed with `Protocol`/`NotImplementedError` | all of `src/polysim/` | S |
| 0.4 | CI safety grep: fail build if `place_order\|create_order\|cancel_order\|post_order\|sign_order` appears outside `tests/fixtures/` | `scripts/ci_safety.sh`, CI config | S |
| 0.5 | SQLite migration runner + `0001_init.sql` matching §6 + the new `known_insiders[]` table **and** `flag_costs`, `owner_address` column on `wallets`, `clock_skew_samples` | `src/polysim/db/` | M |
| 0.6 | `polysim init` + `polysim status` minimal (matches demo panel #1 base state) | `src/polysim/cli.py` | M |
| 0.7 | Config loader — `pydantic-settings` + YAML + `.env`; validates tiered `categories` + `known_insiders[]` from spec §8/§11 | `src/polysim/config.py`, `config.example.yml` | M |
| 0.8 | Test scaffolding — `tests/{unit,integration,backtest}`; fixture schemas documented; xfail markers on known-empty tests | `tests/`, `tests/fixtures/README.md` | S |
| 0.9 | Structured logging (JSON to disk, rich to console) | `src/polysim/utils/logging.py` | S |

**Acceptance**
- `uv run ruff check .` + `uv run mypy src/polysim` clean
- `uv run pytest` runs (xfails allowed, no import errors)
- `polysim init && polysim status` works on a fresh checkout (Windows + Linux)
- CI safety grep rejects a synthetic PR that adds `place_order(...)` to non-test code

**Demo state** — `polysim status` prints the all-zero header from demo panel #1. Nothing streaming yet.

---

## 1. Phase 1 — Ingest & persist

Get the data in. No scoring yet.

| # | Task | Files | Size |
|---|---|---|---|
| 1.1 | Polymarket WS client with reconnect backoff; emits `TradeEvent` to queue | `ingest/polymarket_ws.py` | M |
| 1.2 | Polymarket REST client (get_* only; type-level block on order methods) | `ingest/polymarket_rest.py` | M |
| 1.3 | Polygon enrichment worker (nonce + funding source); Alchemy-rate-limited | `ingest/polygon_rpc.py` | L |
| 1.4 | Market indexer — hourly REST pull, classify via keyword → Haiku fallback, cache to SQLite | `ingest/category.py`, `ingest/polymarket_rest.py` | M |
| 1.5 | DAO layer: async CRUD + upsert semantics on first-sight wallet | `db/dao.py` | M |
| 1.6 | `polysim ingest start` + `ingest backfill --days N` CLI (matches demo panel #1 output exactly) | `cli.py` | S |
| 1.7 | **Proxy wallet resolution** (spec gap G1): add `wallets.owner_address`; REST lookup on first sight | `ingest/polymarket_rest.py`, migration `0002` | M |
| 1.8 | **Funding-source heuristic list** (G2): seed `configs/funding_sources.yml`; match against hot-wallet addresses | `configs/funding_sources.yml`, `ingest/polygon_rpc.py` | S |
| 1.9 | **Clock source** (G3): use WS server timestamp, never local clock; record skew daily to `clock_skew_samples` | `ingest/polymarket_ws.py` | S |
| 1.10 | Unit tests (`respx` for httpx, fixture WS frames) | `tests/unit/ingest/` | M |
| 1.11 | Integration test — replay 100 fixture WS frames, assert 100 trades landed | `tests/integration/test_ingest.py` | M |

**Acceptance** (matches spec §12 Phase 1)
- 24h live ingest with no crash
- ≥ 10,000 trades, ≥ 50 classified markets persisted
- `polysim status` shows live non-zero counters

**Demo state** — panel #1 of `polysim-demo.html` is fully live; panels #2–#5 remain empty.

---

## 2. Phase 2 — Profiler & scoring

Where the detective work happens.

| # | Task | Files | Size |
|---|---|---|---|
| 2.1 | Wallet profiler — idempotent recompute every 5 trades OR 60s | `profiler/wallet_profiler.py` | L |
| 2.2 | `Detector` Protocol + shared evidence types | `scoring/base.py` | S |
| 2.3 | `CategoryInsiderDetector` — binomial p-value; requires ≥ N_MIN_RESOLVED | `scoring/category_insider.py` | M |
| 2.4 | `EventInsiderDetector` — single-trade trigger, logistic combo | `scoring/event_insider.py` | M |
| 2.5 | `FreshWalletDetector` (**G11**: scores only, never flags alone — enforced in composite) | `scoring/fresh_wallet.py` | S |
| 2.6 | `CoordinationDetector` — in-memory NetworkX graph, hourly blob snapshot. **G4**: default 24h window, config-tunable | `scoring/coordination.py` | L |
| 2.7 | `TimingDetector`. **G5**: one-shot script builds per-category late-activity base rates into `meta` table during Phase 1 backfill | `scoring/timing.py`, `scripts/build_timing_baselines.py` | M |
| 2.8 | Composite scorer with §7.4 weights + min-contributors gate | `scoring/composite.py` | M |
| 2.9 | Flag dedup — suppress identical (wallet, market, detector) within 10 min | `scoring/composite.py` | S |
| 2.10 | **G6**: profile staleness guard — profiles > 1h stale → scorer short-circuits | `profiler/wallet_profiler.py`, `scoring/composite.py` | S |
| 2.11 | `polysim flags list/show` — matches demo panels #1 flag lines and #3 flag detail | `cli.py` | M |
| 2.12 | Investigator invocation path wired (per updated spec §18 Q2 — runs from Phase 2; Phase 3 measures its lift) | `investigator/agent.py` | M |
| 2.13 | **G14**: prompt-injection sanitization on wallet display_name + market question before LLM call | `investigator/agent.py` | M |
| 2.14 | Unit tests — every detector's edge cases: no history, all wins, all losses, single category, diverse, NaN guard | `tests/unit/scoring/` | L |
| 2.15 | Property-based tests — detector score monotonicity (hypothesis) | `tests/unit/scoring/test_monotonicity.py` | M |
| 2.16 | Integration — replay 100 fixture trades, assert expected flags | `tests/integration/test_scoring.py` | M |
| 2.17 | **Backtest acceptance stub** — encodes AlphaRacoon, OpenAI browser, Venezuela cluster, Maduro cluster, MrBeast producer; wallet addresses pinned via `known_insiders[]` after Phase 1 backfill | `tests/backtest/test_known_cases.py` | M |

**Acceptance** (spec §12 Phase 2 + spec §18 Q2)
- Oct 2025 – Feb 2026 replay flags all five §13 known-insider cases before their respective resolutions
- < 50 false positives/week across primary categories
- Investigator call path operates (rate-limited to daily cap)

**Demo state** — panels #1, #2 (flag list section), and #3 are live.

---

## 3. Phase 3 — Investigator lift validation

Call path is already wired. This phase **measures** the lift.

| # | Task | Files | Size |
|---|---|---|---|
| 3.1 | Prompt v1 — system + user per §7.5; last-50-trade context | `investigator/prompts.py` | M |
| 3.2 | Anthropic prompt caching on static system + wallet history block; cache key `wallet_addr + trade_count` | `investigator/agent.py` | M |
| 3.3 | Haiku triage vs Opus deep-dive routing — Haiku for composite 5–7, Opus for ≥ 7 (config-selectable) | `investigator/agent.py` | S |
| 3.4 | Daily call cap + fallback-on-composite; UTC midnight reset | `investigator/agent.py` | S |
| 3.5 | **G7**: per-flag cost logging to `flag_costs` table (input/output/cached tokens + cost_cents) | `investigator/agent.py`, migration `0003` | S |
| 3.6 | `polysim investigate <flag_id>` manual (matches demo hint in panel #1 footer) | `cli.py` | S |
| 3.7 | Integration: cached vs uncached assertion on repeat same-wallet query | `tests/integration/test_investigator.py` | M |
| 3.8 | Benchmark harness — run scorer with/without investigator on acceptance replay; compute FP reduction + TP retention | `scripts/measure_investigator_lift.py` | M |

**Acceptance** (spec §12 Phase 3)
- ≥ 70% of known-insider flags labeled `INFORMED`
- ≥ 30% FP reduction vs composite-only
- Daily cost at 100 calls ≤ $3 (Haiku-dominant) or ≤ $30 (Opus-dominant) — new target

**Demo state** — panel #3 shows populated `REASONING`, `RED FLAGS`, `GREEN FLAGS` sections.

---

## 4. Phase 4 — Paper executor & portfolio

The hard-to-fake layer. Fill-model pessimism is load-bearing.

| # | Task | Files | Size |
|---|---|---|---|
| 4.1 | `fill_model.py` — walk-the-book + slippage ticks + log-normal latency sampling from config p50/p95 + partial-fill policy | `paper/fill_model.py` | L |
| 4.2 | `executor.py` — consume approved flags, size copy, call fill model, write `paper_fills` + `paper_positions` | `paper/executor.py` | L |
| 4.3 | `portfolio.py` — bankroll + caps (per-position / per-wallet / per-market) | `paper/portfolio.py` | M |
| 4.4 | Resolution handler — close at $1 / $0 / invalid→0 | `paper/executor.py` | M |
| 4.5 | Kill switches per §14 (drawdown, flag-rate, detector NaN) | `paper/executor.py`, `scoring/composite.py` | M |
| 4.6 | Config snapshot at run start → `run_config.<id>.json` + row | `paper/executor.py` | S |
| 4.7 | `polysim run start/stop/status/list` (matches demo panel #1 `run start` output) | `cli.py` | M |
| 4.8 | **G8**: live orderbook snapshotter during ingest (for realistic historical fills); 2× pessimism multiplier when snapshot absent | `ingest/orderbook_snapshotter.py`, `paper/fill_model.py` | L |
| 4.9 | **G9**: copy-size scaling function `size = base * clamp(composite/5, 0.5, 2.0)` — config-tunable | `paper/executor.py` | S |
| 4.10 | **G10**: optional `mirror_exits` — mirror source-wallet sells proportionally | `paper/executor.py` | M |
| 4.11 | Unit — fill-model edge cases (empty book, depth<size, huge depth, zero/extreme latency) | `tests/unit/paper/` | M |
| 4.12 | Property-based — portfolio invariants (per-wallet exposure ≤ cap, balance conservation) | `tests/unit/paper/test_invariants.py` | M |
| 4.13 | Integration — 7-day backtest end-to-end | `tests/integration/test_7d_backtest.py` | L |

**Acceptance** (spec §12 Phase 4)
- 7-day backtest: complete trade log, no negative balances, no cap breaches, report generated
- All kill switches fire in synthetic test

**Demo state** — `polysim run status` output matches demo panel #1 run-start log; panel #2 dashboard shows live positions.

---

## 5. Phase 5 — Evaluator & reporter

Produce the artifact that answers the project's single question.

| # | Task | Files | Size |
|---|---|---|---|
| 5.1 | `metrics.py` — every §10 metric | `evaluator/metrics.py` | L |
| 5.2 | Sharpe / Sortino / max-DD on daily returns; tested on synthetic series with known answers | `evaluator/metrics.py`, `tests/unit/evaluator/` | M |
| 5.3 | Shapley detector attribution (K=5 → 32 subsets, tractable); cached per run | `evaluator/shapley.py` | M |
| 5.4 | Baseline runners — null (random wallets, matched category/rate) + favorite-mid. Share flag/fill infra with primary | `evaluator/baselines.py` | L |
| 5.5 | **G15**: baseline significance — paired daily-return t-test, α = 0.05, pass/fail on report | `evaluator/significance.py` | M |
| 5.6 | Calibration plot (matplotlib → PNG) + ASCII fallback for CLI | `evaluator/calibration.py` | S |
| 5.7 | Markdown report generator — layout matches demo panel #5 (report section) exactly | `reporter/markdown.py` | M |
| 5.8 | Execution-drag compute (paper P&L minus oracle counterfactual) | `evaluator/metrics.py` | M |
| 5.9 | `polysim report --run N --format md` + `polysim calibrate fill-model` | `cli.py` | S |
| 5.10 | `polysim doctor` — DB integrity + recent trade flow + config validity (operational nicety) | `cli.py` | S |

**Acceptance** (spec §12 Phase 5)
- 90-day backtest → report with every §10 metric, both baseline comparisons with p-values, calibration plot

**Demo state** — panel #5 renders live data from a real run.

---

## 6. Phase 6 — Live paper mode

Glue, polish, endurance test.

| # | Task | Files | Size |
|---|---|---|---|
| 6.1 | `reporter/cli_dashboard.py` — rich TUI per demo panel #2 layout; 500ms live panels, 2s metric panels | `reporter/cli_dashboard.py` | L |
| 6.2 | Popups — `F<id>`, `P<id>`, `W<addr>` overlay handlers in TUI | `reporter/cli_dashboard.py` | M |
| 6.3 | `reporter/telegram.py` — python-telegram-bot, Markdown V2; four alert types matching demo panel #4 exactly | `reporter/telegram.py` | M |
| 6.4 | Daily summary scheduler — 09:00 local, configurable | `reporter/telegram.py` | S |
| 6.5 | Uptime watchdog — if ingest stalls > 5m, Telegram alert | `utils/watchdog.py` | M |
| 6.6 | Reconnection robustness — fault-injection tests on WS drops, Alchemy 429s, SQLite `database is locked` | `tests/integration/test_robustness.py` | M |
| 6.7 | 30-day live run (operator executes) + weekly review | — | XL (calendar) |
| 6.8 | Post-run retro — update weights, promote confirmed wallets into `known_insiders[]` | — | M |

**Acceptance** (spec §12 Phase 6 + spec §18 Q6)
- 30 consecutive days live paper trading, no crashes, ≥ 95% uptime, final report

**Demo state** — the full `polysim-demo.html` view, but with live data.

---

## 7. Cross-phase tracks

### 7.1 Testing discipline
- Every detector, every fill-model branch, every kill switch → unit test.
- Hypothesis property tests on PR (100 examples default, 1000 nightly).
- Integration smoke (100 fixture trades) < 30s, runs on pre-commit.
- Backtest acceptance tests nightly only (slow, full replay).

### 7.2 CI matrix
- Ruff + mypy-strict (non-negotiable)
- Safety grep for order-placement APIs (§0.4)
- Coverage > 80% on `scoring/`, `paper/`, `evaluator/`
- Platform: Linux + Windows (operator box) + macOS (portability check)

### 7.3 Observability
- Structured JSON logs, daily rotation
- Every flag reproducible from stored inputs (spec §14 #5)
- `polysim doctor` (§5.10) for operational checks

### 7.4 Security posture
- `.env` in `.gitignore`; private key fields rejected by config schema
- Alchemy key scoped read-only at provider
- Telegram token never logged

---

## 8. Spec-gap register (consolidated)

| ID | Spec ref | Gap | Phase | Resolution |
|---|---|---|---|---|
| G1 | §7.1 | Polymarket proxy→owner resolution absent | 1 | `wallets.owner_address`; REST lookup on first sight |
| G2 | §7.1 | Funding heuristic data absent | 1 | `configs/funding_sources.yml` seed |
| G3 | §7.1 | Clock source not specified | 1 | Use WS server timestamp; record skew daily |
| G4 | §7.3.4 | Coordination window undefined | 2 | Default 24h, config-tunable |
| G5 | §7.3.5 | Timing base-rate source absent | 2 | Pre-compute from backfill into `meta` |
| G6 | §7.2 | Profile staleness guard absent | 2 | Scorer skips wallets with profile > 1h old |
| G7 | §7.5 | LLM cost accounting absent | 3 | `flag_costs` table + daily roll-up |
| G8 | §9 | Historical orderbook unavailable | 4 | Live snapshotter; 2× pessimism for historical |
| G9 | §7.6 | Copy-size scaling undefined | 4 | `size = base * clamp(composite/5, 0.5, 2.0)` |
| G10 | §7.6 | Only resolution-based exit | 4 | Optional `mirror_exits` on source-wallet sells |
| G11 | §7.3.3 | FreshWalletDetector usage unclear | 2 | Scores only, composite enforces "no solo flag" |
| G12 | §11 | `known_insiders[]` new (spec §18 Q5) | 0 | Config schema updated; table seeded Phase 2 |
| G13 | §18 Q2 | Investigator phase ambiguous | 2/3 | On from Phase 2; lift measured Phase 3 |
| G14 | §7.5 | Prompt injection unaddressed | 2 | Sanitize wallet display_name + market question |
| G15 | §10 | Baseline stat-test unspecified | 5 | Daily-return paired t-test, α=0.05 |
| G16 | — | Windows platform testing | 0 | CI matrix adds Windows 11 runner |

---

## 9. Risk register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Fill model too optimistic → paper P&L doesn't translate to reality | High | Critical | Mandatory 2× pessimism during backtest · live orderbook snapshotter · `calibrate fill-model` command |
| WS disconnect gaps miss flags | Medium | High | Reconnect backoff · daily REST reconciliation of WS-captured trades vs REST history |
| LLM cost blowup | Medium | Medium | Hard daily cap · Haiku-first triage · prompt caching |
| Proxy wallet mis-attribution | Medium | High | G1 resolution · integration test on known proxy cases |
| Overfitting to the five known-insider cases | High | High | Null-strategy baseline is the counter · do **not** tune weights on acceptance window |
| Polymarket API breaking change | Low | High | Client isolated to one module · record/replay fixtures · version-pinned SDK |
| Pop-culture / box-office categories add noise without edge | Medium | Low | Per-category P&L is first-class in report (§6.2) · demote to disabled after two negative 30d runs |
| Invalid-market rate inflates apparent P&L | Medium | Medium | Tracked separately · report fails if > 10% of closes are invalid |
| Windows async/path edge cases | Medium | Low | Operator is on Windows 11 · CI runs Windows matrix |

---

## 10. Execution order & parallelism

```
Phase 0 ──▶ Phase 1 ──▶ Phase 2 ──┬──▶ Phase 3 (lift validation)
                                  └──▶ Phase 4 ──▶ Phase 5 ──▶ Phase 6

Branchable while main track advances:
  • During Phase 2 → draft investigator prompts (lands in Phase 3)
  • During Phase 4 → lay out TUI dashboard (lands in Phase 6)
  • During Phase 5 → stub Telegram formatter (lands in Phase 6)
```

---

## 11. Entry checklist

Before Phase 0 starts:
- [ ] `polysim-spec.md` (with §18 answers) reviewed
- [ ] `polysim-demo.html` reviewed so every surface has a fixed visual target
- [ ] `ANTHROPIC_API_KEY` issued (not blocking until Phase 2.12)
- [ ] Alchemy free-tier key issued (blocks Phase 1.3)
- [ ] Telegram bot created (blocks Phase 6; can defer)
- [ ] Session cadence agreed (≥ 3–5 focused hours / week)

When all checked, next action is: **`uv init polysim && cd polysim`** → start task 0.1.

---

*End of build plan.*
