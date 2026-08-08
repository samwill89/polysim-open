# PolySim Spec Addendum 02: Empirical Priors, Discovery Pipeline & Testing

**Status:** Supplements `polysim-spec.md` and `polysim-spec-addendum-dual-mode.md`
**Date:** April 24, 2026
**Purpose:** Integrate learnings from seven external sources into PolySim's architecture, add a wallet discovery pipeline as a first-class component, and specify a rigorous testing strategy that matches the build quality of the sources we're learning from.

---

## 1. Sources and What Each Contributes

| Source | Primary contribution to PolySim |
|---|---|
| Arcada Prediction Arena (arXiv:2604.07355) | Empirical priors on LLM trading limits; success-factor hierarchy; valuation methodology |
| Polymarket arbitrage paper (arXiv:2508.03474) | Mispricing detection heuristics; order-book-depth-aware sizing; what to *skip* |
| Claude Opus 4.6 Kalshi live log (Prediction Arena dashboard) | Structured-belief agent pattern; per-cycle + long-term plan schema; concrete failure modes |
| Lunar's 4-repo build | Tooling scaffold (poly_data, polymarket-cli, Polymarket/agents); consensus pattern; exit-trigger taxonomy |
| Kaustubh's BTC engine | Lifecycle-per-market architecture; simulation fidelity requirements; debug-via-visualization pattern |
| Karpathy on agent-native tooling | Architectural decision: treat polymarket-cli as a sanctioned tool for ad-hoc agent use |
| Current thread | Wallet discovery pipeline as a first-class component |

Each source is cited in the section where its lessons are applied.

---

## 2. Context: Why the Architecture Is Changing

Three data points materially change the base spec's priors:

1. **Arcada** showed all six frontier LLMs losing on Kalshi (−16% to −30.8%) and averaging −1.1% on Polymarket over a month of live real-capital trading. Research quantity had *zero correlation with performance*; initial prediction accuracy was dominant.
2. **The arbitrage paper** documents $39.7M extracted in 12 months by quantitative traders exploiting logical dependencies between markets, not better forecasting. Top extractor: 4,049 trades averaging $496/trade.
3. **Claude Opus 4.6's Kalshi paper run** shows +613% return that resolves, on inspection, to a single concentrated Israel-Lebanon bet (77.6% of portfolio) during one specific news cycle. Sharpe 0.11 confirms concentration variance, not reproducible process. The *methodology* is worth lifting; the return is not.

The combined implication: **edges on Polymarket empirically come from (a) combinatorial arbitrage needing ms-latency infrastructure, or (b) wallet-level informational asymmetry in specific domains.** PolySim targets (b) and explicitly avoids (a). This sharpens the original thesis rather than replacing it.

---

## 3. New Component: Wallet Discovery Pipeline

**Status in base spec:** missing. **Promoted to:** first-class subsystem, runs before the trading loop.

### 3.1 Rationale

The base spec implied the investigator agent would evaluate wallets on demand. That's wrong for three reasons:

- **Cost.** Running an LLM investigator on 14,000+ wallets is prohibitive and unnecessary.
- **Selection bias measurement.** Without a discovery pipeline, we can't cleanly test whether historical top-P&L wallets predict future P&L (the implicit claim in every "copy these wallets" post).
- **Cohort stability.** We need a stable, pre-committed set of wallets so performance measurements are meaningful. Ad-hoc selection confounds the experiment.

### 3.2 Pipeline Architecture

The discovery pipeline runs nightly and produces a `wallets` table the trading loop reads from. Cohort selection is **niche-first**: the primary cohort is built per target niche, with a secondary overall-edge pool as a supplement.

```
┌──────────────────────────────────────────────────────────────┐
│              Discovery Pipeline (nightly)                    │
│                                                              │
│  1. Clone/pull poly_data (warproxxx/poly_data)               │
│                                                              │
│  2. Feature extraction (pure Polars, no LLM)                 │
│     Per wallet, globally AND per niche:                      │
│     win_rate, trade_count, avg_hold_time, early_exit_ratio,  │
│     avg_size_vs_depth, counterparty_concentration,           │
│     pnl_lifetime, pnl_30d, category_mix                      │
│                                                              │
│  3. Niche tagging                                            │
│     Each market labeled: {aec, ai_labs, creator_econ,        │
│                          general} via keyword + slug match   │
│     Wallets inherit niche tags from markets they traded      │
│                                                              │
│  4. Tier 1 Classifier (pure features, no LLM)                │
│     Produces TWO scores per wallet:                          │
│     - edge_likelihood_global                                 │
│     - edge_likelihood_per_niche (one per target niche)       │
│                                                              │
│  5. Cohort selection (NICHE-FIRST)                           │
│     PRIMARY POOL (target niches):                            │
│     - Top 100 AEC-niche wallets by edge_likelihood_aec       │
│     - Top 100 AI-labs wallets by edge_likelihood_ai_labs     │
│     - Top 100 creator-econ wallets by the equivalent         │
│     SECONDARY POOL (general edge):                           │
│     - Top 200 overall by edge_likelihood_global              │
│       (with primary-pool wallets deduplicated out)           │
│     TOTAL: ~500 wallets, frozen per experiment               │
└──────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌──────────────────────────────────────────────────────────────┐
│              Trading Loop (continuous)                       │
│                                                              │
│  For each market in scan:                                    │
│  - Tag market's niche                                        │
│  - Find cohort wallets with positions here                   │
│    - Niche-matched wallets (primary) get higher weight       │
│    - General-edge wallets (secondary) get lower weight       │
│  - Tier 2 Investigator runs → structured belief (§5)         │
│  - Decision gate (§5.4): signal is INPUT, not trigger        │
│  - If belief passes gate → size per dual-mode rules          │
└──────────────────────────────────────────────────────────────┘
```

### 3.3 Module Layout

Aligning with base spec's `src/polysim/...` structure:

```
src/polysim/
├── discovery/                      # NEW subsystem
│   ├── __init__.py
│   ├── poly_data_sync.py           # clone/pull poly_data repo
│   ├── features.py                 # wallet feature extraction (Polars)
│   ├── classifier.py               # Tier 1 classifier
│   ├── cohort.py                   # frozen cohort selection per experiment
│   └── schema.sql                  # wallets, wallet_features tables
├── agents/
│   ├── investigator.py             # Tier 2, per-market LLM analysis
│   └── belief_schema.py            # structured belief format (§5)
├── trading/
│   ├── loop.py                     # main cycle orchestration
│   ├── consensus.py                # cohort-wallet + investigator aggregation
│   └── sizing.py                   # dual-mode position sizing
├── portfolio/
│   ├── valuation.py                # bid-price MTM (enforced)
│   ├── settlement.py               # realistic settlement delay sim
│   └── ledger.py
├── experiment/
│   ├── hypotheses.py               # pre-registered tests (§8)
│   ├── baselines.py                # four baselines
│   └── reporting.py                # end-of-run statistics
└── io/
    ├── clob_client.py              # py-clob-client wrapper (read-only)
    └── cli_bridge.py               # optional polymarket-cli bridge (§7)
```

### 3.4 Frozen Cohorts

Once a 30-day experiment begins, the cohort is frozen. No wallets added, none removed. This is how we avoid the survivorship-bias trap that makes "top 20 made more than the bottom 13,000 combined" meaningless — pre-commit to the cohort, measure forward, report honestly.

### 3.5 Why Niche-First

The base spec identified AEC software, AI lab launches, and creator economy as target niches because of domain expertise (genuine knowledge is a moat in prediction markets). The discovery pipeline honors this by making niche-matched wallets the *primary* cohort, not a stratification layer on a general pool.

The reasoning is mechanical: a wallet that hits 70% win rate across 300 general-market trades likely has a style edge (e.g., good at spotting mispriced favorites). A wallet that hits 70% win rate across 300 *AEC-software market* trades likely has an informational edge (they know the industry). For PolySim's thesis — that we can detect informational asymmetry via behavioral signals — the second wallet is far more interesting. The Tier 1 classifier should see both, but the cohort allocation should weight the second higher.

Concretely:
- **Primary pool (niche-matched):** 300 wallets, 100 per target niche. Selected by `edge_likelihood_per_niche` where the wallet has ≥ 50 trades in that niche.
- **Secondary pool (general edge):** 200 wallets by `edge_likelihood_global`, with primary-pool wallets deduplicated out.
- Wallets in the primary pool are weighted 1.5× in consensus calculations (see §5.4).

### 3.6 Niche Tagging Methodology

Niche tags are assigned to markets at scan time, not at discovery time, so that new markets can be tagged without retraining. Each target niche has a keyword and slug-pattern list:

```python
NICHE_TAGS = {
    "aec": {
        "keywords": ["autodesk", "procore", "bentley", "bim", "construction",
                     "architecture engineering", "cad software", "revit", ...],
        "slug_patterns": [r"autodesk-.*", r"procore-.*", ...],
    },
    "ai_labs": {
        "keywords": ["openai", "anthropic", "deepmind", "xai", "gpt",
                     "claude", "gemini", "model release", "ai lab", ...],
        "slug_patterns": [r".*-gpt-\d+.*", r"claude-.*-release", ...],
    },
    "creator_econ": {
        "keywords": ["youtube", "tiktok", "substack", "creator",
                     "streamer subs", "patreon", ...],
        "slug_patterns": [r"mr-beast-.*", r".*-youtube-subs-.*", ...],
    },
}
```

Tags are multi-label (a market can be both AI-labs and general). The keyword lists are maintained in `src/polysim/discovery/niche_tags.py` and versioned — any change requires bumping the experiment version and re-running discovery, to prevent silent cohort drift.

---

## 4. Updates to Valuation, Settlement, and Risk

### 4.1 Bid-Price Mark-to-Market (Arcada §5.3.1)

Mid-price MTM systematically overstates performance by the bid-ask spread (typically 2–5¢ on Polymarket).

**Rule:** All account-value calculations in `src/polysim/portfolio/valuation.py` use bid prices for open positions.

```python
# Required:
position_value = quantity * current_bid_price

# Prohibited in performance reporting:
position_value = quantity * current_mid_price
```

**Enforcement:** CI grep extends from base spec's "no live orders" to also flag `mid_price` in valuation paths. Unit tests in `tests/portfolio/test_valuation.py` assert a position bought at 50¢ with bid=48¢/ask=52¢ is valued at 48¢.

### 4.2 Settlement Timing Simulation (Kaustubh §Under the Hood, Opus 4.6 §Risk Notes)

Polymarket reality:
- Orders match off-chain via CLOB, settle on-chain on Polygon (~2s block time).
- Shares are not sellable immediately after buy — full settlement cycle required.
- Large positions can lock trading capacity when settlement is delayed (Opus 4.6: "5+ consecutive cycles").

**Rule:** The paper-trading simulator must model:
1. Minimum 2-block (~4s) delay between buy fill and shares-available-to-sell.
2. "Settlement pending" portfolio state with partial capacity lock.
3. Configurable resolution-to-payout delay (default 0–300s, configurable up to 5 cycles for stress testing).

**Enforcement:** `tests/portfolio/test_settlement.py` includes scenarios where instant round-trips must fail and where 5-cycle locks must reproduce Opus 4.6's capacity-locked behavior.

### 4.3 Resolution Criteria Risk as a First-Class Field (Opus 4.6)

From Opus 4.6's belief log: its 1,369-share YES on "Israel x Lebanon diplomatic meeting by April 19" resolved NO for −$1,096 because the April 14 meeting *"counted for the April 26 and April 30 deadlines but NOT for the April 19 deadline."* Its own note: *"Resolution criteria interpretation can be strict and unexpected."*

**Rule:** `Market.resolution_risk_score: float [0.0, 1.0]` is required, produced by the investigator, with higher scores for:
- UMA optimistic oracle markets with subjective criteria
- Geographic specificity (e.g., "Greater Beirut" map definitions)
- Exact-day deadlines where adjacent days may resolve differently
- Prior disputed resolutions from the same resolver

**Thresholds:**
- Systematic mode: skip markets with `resolution_risk_score > 0.3`
- Degen mode: accept up to 0.6

### 4.4 Order Book Depth Awareness (arbitrage paper §Position Sizing)

The arbitrage paper caps at 50% of available depth. Kaustubh and Lunar both independently note position > book depth moves the market against the trader.

**Rule:** Before executing any simulated order, check current top-3-levels depth.
- Systematic mode: cap at 25% of top-3 depth
- Degen mode: cap at 40%
- If intended size exceeds cap: split across cycles or size down, never single-shot through depth.

---

## 5. Investigator Agent Restructure

### 5.1 Two-Tier Structure (Arcada §8.1 + Lunar §Step 2)

Arcada's cleanest finding: research quantity had zero correlation with performance; initial prediction accuracy was dominant. Implication: **optimize for fast, disciplined first reads, not exhaustive research.**

**Tier 1 — Classifier.** Runs during nightly discovery. Pure features, no LLM. Produces `edge_likelihood` per wallet. Near-zero cost per wallet.

**Tier 2 — Investigator.** Runs during trading loop only when ≥1 cohort wallet is in a market. Uses Claude API. Produces structured belief. Cost bounded by cohort activity.

### 5.2 Structured Belief Schema (adapted from Opus 4.6 belief log)

Opus 4.6's belief log is the most useful methodological artifact we have — confidence-scored beliefs in categories (`risk_assessment`, `event_analysis`, `trading_strategy`, `market_structure`) with explicit EV math and resolution-risk flags. We adopt this format.

```python
# src/polysim/agents/belief_schema.py
class Belief(BaseModel):
    market_id: str
    category: Literal["event_analysis", "risk_assessment",
                      "trading_strategy", "market_structure"]
    confidence: float  # [0.0, 1.0]
    estimated_true_probability: float  # [0.0, 1.0]
    resolution_risk_score: float  # [0.0, 1.0]
    expected_value_per_contract: float
    rationale: str  # ≤ 1 sentence
    cohort_wallets_involved: list[str]
    schema_version: str  # semver
    timestamp: datetime

class CyclePlan(BaseModel):
    cycle_id: str
    portfolio_snapshot: PortfolioSnapshot
    ready_trades: list[TradeIntent]  # ranked by EV
    blockers: list[str]  # e.g., "capacity locked: awaiting settlement"
    opportunity_cost_estimate: float  # $/day being lost if blocked
    priority_actions: list[str]  # ordered
    created_at: datetime

class LongTermPlan(BaseModel):
    target_date: date
    phases: list[Phase]
    lessons_learned: list[str]  # persisted across cycles
    created_at: datetime
```

Artifacts logged to `logs/beliefs/`, `logs/plans/cycle/`, `logs/plans/long_term/`. Inputs to end-of-experiment analysis.

### 5.3 What the Investigator Does NOT Do

Explicitly out of scope, to prevent drift toward Arcada's failure modes:
- No open-ended per-market web research (Arcada: research quantity uncorrelated with performance)
- No second-guessing the classifier's cohort selection (cohort is frozen)
- No order issuance (separate module handles execution)
- No runtime schema modification (schema is versioned and tested)

### 5.4 Decision Gate: Wallet Signals Are Inputs, Not Triggers

**Core principle:** A cohort wallet entering a market is a *signal worth investigating*, not a signal to act. The investigator's job is to form an independent judgment using the wallet activity as one input among several, and the consensus layer's job is to combine those judgments with explicit gates that can veto a trade even when wallets agree.

This is the single most important distinction between PolySim and a naive copy-trading bot. Blind copy-trading is what fails in the wild (per Arcada's -22.6% Kalshi average and per the arbitrage paper's note that copy-trading fast wallets makes you exit liquidity, not an arbitrageur). PolySim's thesis is that *judgment layered on top of wallet signals* is where the edge lives, if it exists at all.

**Decision flow for every candidate trade:**

```
1. Wallet signal detected
   - ≥1 cohort wallet has an open position in market M
   - Compute signal_strength = Σ(wallet_weight × wallet_edge_likelihood)
     where niche-matched wallets carry 1.5× weight (see §3.5)

2. Investigator invocation (gated on signal_strength ≥ min_investigate_threshold)
   - Produces Belief object (§5.2)
   - Includes: estimated_true_probability, resolution_risk_score,
     expected_value_per_contract, confidence

3. Decision gate — ALL of the following must pass (systematic mode):
   a) belief.confidence ≥ 0.6
   b) belief.resolution_risk_score ≤ 0.3
   c) belief.expected_value_per_contract > spread_cost_per_contract
      (EV must exceed round-trip bid-ask cost; below this, the edge is spread noise)
   d) belief.estimated_true_probability is directionally consistent with
      cohort wallet positioning (if cohort is YES and investigator estimates
      NO has higher true probability, investigator VETOES)
   e) market depth check passes (§4.4)
   f) no correlated positions already held (concentration check: ≤2 positions
      per niche, ≤15% account value per geopolitical theme)

4. If all gates pass → size per dual-mode rules, execute (simulated)
   If any gate fails → log the rejection with reason, do not trade
```

**Degen mode relaxes gates (b), (c), and (f)** but keeps (a) and (d). The investigator's veto on directional inconsistency is non-negotiable even in degen mode — we never trade against our own best estimate of the true probability.

**Rejection logging is load-bearing.** Every rejected candidate is written to `logs/decisions/rejections.jsonl` with the full gate-by-gate breakdown. At end-of-experiment, rejection data answers: how often did the investigator veto a cohort signal? When it did, did the trade we didn't take go on to be profitable or not? This is how we measure whether the judgment layer is adding value vs. just acting as friction.

**Conflict between wallet signal and investigator judgment is the interesting case.** If cohort wallets with high edge_likelihood are YES on a market and the investigator says NO, that is *data*, not an error. Log it prominently; review at end-of-experiment. Patterns in these conflicts are where we learn whether the classifier's features are actually capturing informational edge or just style.

---

## 6. Exit Strategy Taxonomy (Lunar §Step 4 + Opus 4.6 §trading_strategy)

Lunar identifies three exit triggers; Opus 4.6 adds a fourth. We treat all four as *testable hypotheses*:

1. **Target hit:** Exit at 85% of expected move toward fair value.
2. **Volume spike:** 3× normal 10-min volume → "smart money exiting" signal.
3. **Stale thesis:** 24h elapsed without significant price change → exit.
4. **Forecast update:** For observable-forecast markets (weather, economic data), exit when forecast shifts against position before expiration.

**Rule:** The trading loop implements all four with feature flags. The experiment runs with all four enabled but logs counterfactual outcomes (what would have happened if trigger X were disabled) to make §8 hypotheses testable.

---

## 7. Tooling Decisions (Karpathy + Lunar §Step 0–1)

### 7.1 poly_data as Authoritative Historical Source

The `warproxxx/poly_data` repo is cloned and pulled nightly as part of discovery. We don't re-implement trade history scraping. If poly_data goes stale, documented fallback is direct Polygon RPC → CTF contract event logs.

### 7.2 polymarket-cli for Ad-Hoc Agent Tasks (Karpathy)

Polymarket's official CLI is agent-native. We don't use it in the production trading loop (py-clob-client is sufficient), but we expose a sanctioned wrapper in `src/polysim/io/cli_bridge.py` for ad-hoc agent use during development. Example: "Claude, show me the top-volume markets right now" during manual review.

**Safety:** The CLI bridge is read-only by contract — exposes `markets list`, `clob book`, `clob midpoint` but not order-placement commands. Same CI grep check that bans live orders bans CLI order commands.

### 7.3 Polymarket/agents and Polymarket-Trading-Bot

Polymarket/agents (official, MIT) — reasonable reference for how Polymarket thinks about agent integration; we read patterns but don't depend on it as runtime. Polymarket-Trading-Bot (dylanpersonguy) — not officially maintained by Polymarket; we don't import from it, but its exit-trigger taxonomy informed §6.

---

## 8. Experiment Design

### 8.1 Pre-Registered Hypotheses

The 30-day paper-trading experiment tests five falsifiable hypotheses:

| ID | Hypothesis | Null | Pre-registered α |
|---|---|---|---|
| H1 | Systematic mode (cohort + 2% sizing + resolution-risk ≤0.3) produces returns > 0 net of bid-ask costs | Returns indistinguishable from zero | 0.05 |
| H2 | Degen mode produces higher mean returns than systematic, with higher variance | Means equal after variance adjustment | 0.10 |
| H3 | Niche-matched primary cohort (AEC / AI labs / creator econ) outperforms general-edge secondary cohort | No niche effect after controlling for edge_likelihood | 0.10 |
| H4 | Tier 1 `edge_likelihood` correlates with forward 30-day wallet P&L at ρ > 0.3 | ρ ≤ 0.3 | 0.05 |
| H5 | Top-500-by-lifetime-P&L wallets predict forward 30-day P&L (the "Lunar hypothesis") | Historical P&L has no forward predictive value after controlling for trade count | 0.05 |
| H6 | Investigator judgment adds value over blind cohort-following (the "judgment layer" hypothesis) | Trades that passed all decision gates have no better forward P&L than trades vetoed by the investigator | 0.05 |

H4, H5, and H6 are the scientifically interesting ones — they produce reusable artifacts (the classifier, or a clean refutation, or evidence that the judgment layer is or is not load-bearing) even if H1/H2 both fail.

**H6 is the test that PolySim is not just copy-trading with extra steps.** The experiment logs every rejected candidate (§5.4). At end-of-experiment we reconstruct what each rejected trade *would* have returned if it had been taken at the cohort-signal-detection moment. If the investigator's veto consistently identified losers, the judgment layer is real value. If vetoed trades performed as well as taken trades, the judgment layer is friction and we should simplify.

### 8.2 Four Baselines

Every hypothesis is measured against all four where applicable:

1. **Random cohort:** Random wallets with >100 lifetime trades, no edge filter.
2. **Top-historical-P&L:** Top 20 by lifetime P&L, no other filter. Explicit test of Lunar's implicit strategy.
3. **Buy-and-hold consensus:** Buy the side the market currently favors. The "no edge" null.
4. **Inverse classifier:** Copy-trade wallets with `edge_likelihood < 0.3`. Isolates whether the classifier (vs. wallet-following itself) is the signal.

### 8.3 Twitter-Thread Claims as Measurement Targets

| Claim | Source | Measurement | Pass condition |
|---|---|---|---|
| Volume spike = smart money exit | Lunar | Does 3× avg 10-min volume predict negative 30-min forward return? | ρ < −0.15 with p<0.05 |
| 85% of expected move is optimal exit | Lunar | Compare fixed-85% vs. settlement vs. time-decay exits | 85% higher mean PnL at p<0.10 |
| Quarter-Kelly + consensus kills 40% of losers | Lunar | Simulate with/without consensus filter on cohort | Kill rate within 30–50% band |
| Sports markets no edge (52% WR) | Lunar | Per-category WR on our cohort | Sports WR ≤ 55% |
| Weather markets unreliable | Arcada + Opus 4.6 | Already confirmed; excluded via category filter | N/A |
| Crypto favorites efficiently priced | Opus 4.6 | Per-market EV on crypto favorites | Mean EV ≤ $0.05/contract |

Most of these will probably refute. That is the point — turn noise into measurement.

---

## 9. Testing Strategy

### 9.1 Unit Test Coverage Targets (CI-enforced)

- `portfolio/`: 95% line coverage. Silent valuation/settlement bugs invalidate the experiment.
- `discovery/`: 90%. Feature extraction bugs poison the classifier.
- `agents/`: 80%. LLM outputs are harder to test; focus on schema validation and edge cases.
- `trading/`: 90%.
- `experiment/`: 95%. Statistical test implementations must be correct; use scipy/statsmodels rather than hand-rolling.

### 9.2 Golden-Path Tests

End-to-end tests with fixed seeds reproducing known historical scenarios:

- **Opus 4.6 replay:** Replay April 14–26 Israel-Lebanon market data through our system with a simplified cohort. Assert detection of concentration risk (>15% in one position triggers flag in systematic mode) and resolution-criteria risk on the April 19 market.
- **Arcada cross-check:** Replay Arcada's published Polymarket results for one model across Feb 9–Mar 9, 2026. Our bid-price MTM should match Arcada's figures within rounding. Mismatch means our valuation is wrong.
- **Arbitrage detection null:** Feed a simulated arbitrage opportunity (YES + NO < $1.00 across correlated markets). Assert the system flags it `arb_detected, skip` rather than attempting to trade.
- **Decision gate veto test:** Construct a synthetic scenario where cohort wallets are strongly YES on a market but the investigator's belief says NO is the true direction. Assert the trade is vetoed, the rejection is logged with reason `directional_inconsistency`, and no position is opened. This is the H6 mechanism under test.
- **Niche weighting test:** With a synthetic 5-wallet cohort (2 niche-matched at 1.5× weight, 3 general at 1.0×), verify consensus calculations produce the expected weighted signal_strength. Catches silent bugs in the weighting logic that would otherwise only surface as degraded experiment results.

### 9.3 Property-Based Tests (Hypothesis library)

- Portfolio value monotonic in position quantity at fixed price
- Bid-price MTM ≤ mid-price MTM ≤ ask-price MTM always
- Settlement delay ≥ 2 blocks for every simulated fill
- `edge_likelihood` stable under feature-order permutations (classifier isn't accidentally using row order as a feature)

### 9.4 Simulation Fidelity Tests (Kaustubh §Strategy)

Kaustubh's key point: a strategy working in simulation must work in production, which means the simulator has to model latency, partial fills, cancellations, post-fill settlement delay. We add a **fidelity test suite**:

- Sample 100 real Polymarket orders from poly_data with known fill outcomes
- Replay through our simulator with matching market state
- Assert fill price within 1¢ of recorded, fill timing within 2 blocks, post-fill sellable-timestamp matches

If fidelity tests fail, the simulator is lying and experiment results are invalid.

### 9.5 CI Checks (extending base spec)

```bash
# Existing (base spec):
grep -r "place_order\|submit_order\|execute_trade" src/ | grep -v "simulate_" && exit 1

# New:
grep -r "mid_price" src/polysim/portfolio/valuation.py && exit 1
grep -r "polymarket.*order\|clob.*place" src/polysim/io/cli_bridge.py && exit 1

# Schema validation:
python -m polysim.agents.belief_schema --validate-all-logs || exit 1

# Statistical test correctness:
pytest tests/experiment/test_hypotheses.py -v --strict || exit 1
```

### 9.6 Pre-Experiment Validation Run

Before the 30-day experiment, a 72-hour "dry run" exercises every code path:

- Discovery pipeline produces a wallet cohort of expected size
- Trading loop cycles at expected frequency
- Every hypothesis test produces a well-formed null result on 72h data
- Every log file is written in the schema-validated format
- End-of-run reporting produces a complete markdown report without errors

Only if the dry run passes does the 30-day experiment begin. This prevents discovering at day 15 that a log file has been silently malformed for two weeks.

---

## 10. What This Addendum Does NOT Change

- **Polymarket-only scope.** Kalshi remains off-table.
- **Read-only py-clob-client.** polymarket-cli is for ad-hoc dev tasks, not the trading loop.
- **Live order placement architecturally prohibited.** CI enforcement extends; the rule is unchanged.
- **Personal-use scope.** No distribution, no live capital, paper-only.
- **Target niches.** AEC software, AI lab launches, creator economy — now elevated to H3.
- **Dual-mode A/B architecture.** Systematic vs. degen remains core; this addendum sharpens thresholds but doesn't change the structure.

---

## 11. Open Questions Requiring Resolution Before Build

1. **Arbitrage opportunities in paper trading.** The simulator probably can't realistically model sub-second arbitrage races. Proposed: detect arb-pattern markets in scanning and skip with a logged `arb_detected` flag. Document that PolySim does not claim to measure arb strategy performance.

2. **poly_data freshness.** Community-maintained, could go stale. Proposed: staleness check in the nightly pipeline; if latest trade timestamp >24h old, alert and fall back to direct CTF contract event scraping for the gap.

3. **Cohort size tradeoff.** Top 500 by edge_likelihood may be too many or too few. Proposed: run dry run with 500, measure discovery cost and signal-to-noise, adjust before experiment.

4. **Experiment extension policy.** Arcada's 57-day run showed leaderboard reshuffling after day 34. If H1 at day 30 is ambiguous (p between 0.05 and 0.20), do we extend to 60 days? Proposed: pre-commit to extending in the ambiguous band, stopping in clearly-null (p>0.5) or clearly-positive (p<0.01) bands.

5. **Belief schema versioning.** Once logging beliefs, the schema is load-bearing for reproducibility. Proposed: semver the schema; include schema version in every logged belief (already in §5.2).

These are pre-build decisions. Resolving them now is cheaper than patching mid-experiment.
