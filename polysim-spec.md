# PolySim — Prediction Market Insider Detection & Copy-Trading Simulator

**Version:** 0.1 (spec)
**Audience:** Claude Code, executing a greenfield implementation
**Owner:** the operator (personal research project)

---

## 0. One-paragraph summary

PolySim ingests Polymarket trade data, profiles wallets, flags those exhibiting insider-like patterns using a composite rule-based scorer (optionally gated by an LLM investigator agent), and simulates copy-trading them with paper money. It produces comprehensive metrics so the operator can decide whether the strategy would have worked in reality. **There is no live-execution pathway. None. Ever. See §16.**

---

## 1. Objective

Answer, with statistical rigor, a single question:

> *"If I had copy-traded wallets flagged by this detector, under realistic execution assumptions, would I have made money over a representative period — and for which niches, at which sizing, with which detector weights?"*

Success is not profit. Success is producing reliable, auditable answers to that question.

---

## 2. Scope — HARD constraints

**In scope**
- Read-only integration with Polymarket public CLOB / Gamma / Data APIs
- Read-only Polygon RPC access for wallet metadata (nonce, funding trail)
- Local SQLite database for all state
- Rule-based and statistical insider detectors
- Optional LLM "investigator" agent using the Claude API (read-only, advisory)
- Paper-trading simulator with realistic fill, slippage, and latency models
- Backtest mode (replay historical trades) and live paper mode (watch live, paper-execute)
- CLI dashboard and optional Telegram alerts
- Per-signal, per-wallet, per-category metrics

**Out of scope — must not be built**
- Any real order placement to any exchange
- Private key handling of any kind
- Selling access, hosting for others, or any multi-tenant feature
- A web UI
- Anything touching Kalshi live orders (read-only market data only, if at all)

---

## 3. Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                         LIVE / REPLAY MODE                        │
│                                                                   │
│  ┌──────────┐    ┌────────────┐    ┌──────────┐    ┌──────────┐  │
│  │ Ingestor │───▶│  Profiler  │───▶│  Scorer  │───▶│ Flagger  │  │
│  └──────────┘    └────────────┘    └──────────┘    └────┬─────┘  │
│        │               │                 │              │         │
│        ▼               ▼                 ▼              ▼         │
│  ┌──────────────────────────────────────────────────────────┐    │
│  │                    SQLite (single source of truth)        │    │
│  └──────────────────────────────────────────────────────────┘    │
│                                                       │           │
│                                                       ▼           │
│                                            ┌────────────────────┐ │
│                                            │ Investigator Agent │ │
│                                            │    (Claude API)    │ │
│                                            └─────────┬──────────┘ │
│                                                       │           │
│                                                       ▼           │
│                                            ┌────────────────────┐ │
│                                            │  Paper Executor    │ │
│                                            └─────────┬──────────┘ │
│                                                       │           │
│                                                       ▼           │
│                                            ┌────────────────────┐ │
│                                            │     Evaluator      │ │
│                                            └─────────┬──────────┘ │
│                                                       ▼           │
│                                            ┌────────────────────┐ │
│                                            │   Reporter (CLI)   │ │
│                                            └────────────────────┘ │
└──────────────────────────────────────────────────────────────────┘
```

All modules are Python async. Communication is via internal `asyncio.Queue`s — no Redis / Kafka. Persistence is SQLite. Single process, single user.

---

## 4. Tech stack (pinned)

| Concern | Choice | Reason |
|---|---|---|
| Language | Python 3.12 | Ecosystem + async maturity |
| Package manager | `uv` | Fast, reproducible |
| DB | SQLite via `aiosqlite` | Single file, portable, enough for one user |
| HTTP | `httpx` | Async-native |
| WebSocket | `websockets` | Polymarket CLOB WS |
| CLI/TUI | `typer` + `rich` | Quick to build, nice output |
| Config | `pydantic-settings` + YAML | Typed config |
| Testing | `pytest`, `pytest-asyncio`, `hypothesis` | Property-based tests for scoring math |
| Lint/format | `ruff`, `mypy --strict` | Non-negotiable |
| Polymarket client | `py-clob-client` (READ ONLY) | Official, but only `get_*` methods permitted |
| Polygon RPC | `web3.py` against Alchemy free tier | Wallet metadata only |
| LLM | `anthropic` SDK, `claude-opus-4-7` for investigator, `claude-haiku-4-5` for bulk classification | Latest available |
| Alerts | `python-telegram-bot` (optional) | Operator notifications |

**Dependency hard rule:** The implementation must not import any Polymarket SDK function that places, cancels, or signs an order. A CI check greps for `place_order`, `create_order`, `cancel_order`, `post_order` and fails the build if found outside `tests/` fixtures.

---

## 5. Directory structure

```
polysim/
├── pyproject.toml
├── README.md
├── config.example.yml
├── .env.example
├── docs/
│   ├── spec.md                  # this file
│   ├── detectors.md             # detector math writeups
│   └── runbook.md               # operator handbook
├── src/
│   └── polysim/
│       ├── __init__.py
│       ├── cli.py               # typer entry: `polysim` command
│       ├── config.py            # pydantic-settings
│       ├── db/
│       │   ├── __init__.py
│       │   ├── schema.sql
│       │   ├── migrations/
│       │   └── dao.py           # async data access
│       ├── ingest/
│       │   ├── polymarket_ws.py
│       │   ├── polymarket_rest.py
│       │   └── polygon_rpc.py
│       ├── profiler/
│       │   └── wallet_profiler.py
│       ├── scoring/
│       │   ├── base.py          # Detector protocol
│       │   ├── category_insider.py
│       │   ├── event_insider.py
│       │   ├── fresh_wallet.py
│       │   ├── coordination.py
│       │   ├── timing.py
│       │   └── composite.py
│       ├── investigator/
│       │   └── agent.py         # Claude-based filter
│       ├── paper/
│       │   ├── executor.py      # simulated fills
│       │   ├── fill_model.py    # slippage, latency, fees
│       │   └── portfolio.py     # positions, P&L
│       ├── evaluator/
│       │   ├── metrics.py
│       │   └── backtest.py
│       ├── reporter/
│       │   ├── cli_dashboard.py
│       │   └── telegram.py
│       └── utils/
│           ├── logging.py
│           └── time.py
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── backtest/
│   └── fixtures/
│       ├── trades_sample.jsonl
│       ├── wallets_sample.jsonl
│       └── markets_sample.jsonl
└── scripts/
    ├── seed_historical.py       # hydrate SQLite from Polymarket for backtest
    └── replay.py                # replay a time window into the live pipeline
```

---

## 6. Data model (SQLite schema)

All timestamps are UTC, stored as ISO 8601 strings (SQLite `TEXT`). Money is stored as integer USDC cents to avoid float drift. All schema changes go through numbered migrations under `src/polysim/db/migrations/`.

```sql
-- Markets
CREATE TABLE markets (
    id TEXT PRIMARY KEY,              -- Polymarket market id (condition_id)
    slug TEXT NOT NULL,
    question TEXT NOT NULL,
    category TEXT,                    -- 'ai', 'aec', 'creator', 'macro', 'other'
    created_at TEXT NOT NULL,
    resolves_at TEXT,
    resolved_outcome TEXT,            -- 'YES', 'NO', 'INVALID', NULL if unresolved
    resolved_at TEXT,
    daily_volume_usd INTEGER,         -- rolling 24h, cents
    metadata_json TEXT
);

CREATE INDEX idx_markets_category ON markets(category);
CREATE INDEX idx_markets_resolves_at ON markets(resolves_at);

-- Wallets
CREATE TABLE wallets (
    address TEXT PRIMARY KEY,
    first_seen_at TEXT NOT NULL,
    nonce INTEGER,
    funding_source TEXT,              -- 'binance', 'coinbase', 'unknown', ...
    funding_first_deposit_at TEXT,
    lifetime_volume_cents INTEGER DEFAULT 0,
    lifetime_trades INTEGER DEFAULT 0,
    display_name TEXT,                -- optional Polymarket username
    metadata_json TEXT,
    last_profiled_at TEXT
);

-- Trades (one row per fill)
CREATE TABLE trades (
    id TEXT PRIMARY KEY,
    wallet_address TEXT NOT NULL REFERENCES wallets(address),
    market_id TEXT NOT NULL REFERENCES markets(id),
    side TEXT NOT NULL CHECK(side IN ('BUY', 'SELL')),
    outcome TEXT NOT NULL CHECK(outcome IN ('YES', 'NO')),
    size_shares INTEGER NOT NULL,
    price_cents INTEGER NOT NULL,     -- 0..100
    timestamp TEXT NOT NULL,
    tx_hash TEXT
);

CREATE INDEX idx_trades_wallet ON trades(wallet_address, timestamp);
CREATE INDEX idx_trades_market ON trades(market_id, timestamp);
CREATE INDEX idx_trades_timestamp ON trades(timestamp);

-- Wallet profile snapshots (denormalized, recomputed)
CREATE TABLE wallet_profiles (
    wallet_address TEXT NOT NULL REFERENCES wallets(address),
    as_of TEXT NOT NULL,
    total_markets INTEGER,
    resolved_markets INTEGER,
    wins INTEGER,
    losses INTEGER,
    win_rate REAL,
    total_pnl_cents INTEGER,
    categories_json TEXT,             -- {'ai': 12, 'aec': 0, ...}
    category_exclusivity REAL,        -- 0..1, Herfindahl on categories
    avg_entry_to_resolution_hours REAL,
    features_json TEXT,               -- any additional features the profiler emits
    PRIMARY KEY (wallet_address, as_of)
);

-- Flags (raised by detectors / scorer)
CREATE TABLE flags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet_address TEXT NOT NULL,
    market_id TEXT NOT NULL,
    trade_id TEXT,                     -- triggering trade, nullable
    detector_name TEXT NOT NULL,
    raw_score REAL NOT NULL,
    composite_score REAL,
    components_json TEXT NOT NULL,     -- per-detector breakdown
    investigator_verdict TEXT,         -- 'INFORMED', 'LUCKY', 'UNCLEAR', NULL
    investigator_reasoning TEXT,
    created_at TEXT NOT NULL,
    acted_on INTEGER DEFAULT 0,
    UNIQUE(wallet_address, market_id, detector_name, created_at)
);

CREATE INDEX idx_flags_created ON flags(created_at);
CREATE INDEX idx_flags_wallet ON flags(wallet_address);

-- Paper portfolios (one row per simulation run)
CREATE TABLE paper_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    config_json TEXT NOT NULL,
    starting_balance_cents INTEGER NOT NULL,
    current_balance_cents INTEGER NOT NULL,
    notes TEXT
);

-- Paper positions
CREATE TABLE paper_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES paper_runs(id),
    market_id TEXT NOT NULL,
    outcome TEXT NOT NULL,
    size_shares INTEGER NOT NULL,
    avg_entry_price_cents INTEGER NOT NULL,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    realized_pnl_cents INTEGER,
    source_flag_id INTEGER REFERENCES flags(id),
    source_wallet TEXT,
    status TEXT CHECK(status IN ('OPEN', 'CLOSED', 'RESOLVED'))
);

-- Paper fills (the simulated "execution")
CREATE TABLE paper_fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES paper_runs(id),
    position_id INTEGER NOT NULL REFERENCES paper_positions(id),
    side TEXT NOT NULL CHECK(side IN ('BUY', 'SELL')),
    size_shares INTEGER NOT NULL,
    fill_price_cents INTEGER NOT NULL,
    intended_price_cents INTEGER NOT NULL,  -- what we wanted
    slippage_cents INTEGER NOT NULL,        -- fill - intended
    latency_ms INTEGER NOT NULL,
    fee_cents INTEGER NOT NULL,
    timestamp TEXT NOT NULL
);

-- Evaluation metrics (cached for dashboarding)
CREATE TABLE metrics_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES paper_runs(id),
    as_of TEXT NOT NULL,
    metrics_json TEXT NOT NULL
);
```

---

## 7. Module specifications

### 7.1 Ingestor (`polysim.ingest`)

**Responsibilities**
1. Subscribe to Polymarket CLOB WebSocket for new fills; write each fill to `trades`.
2. Nightly backfill for any market in watched categories via Polymarket Data API.
3. On first sighting of any wallet, enqueue it for Polygon RPC enrichment (nonce, funding).
4. Index new markets hourly; classify them into categories (see §8).

**Key interfaces**

```python
class TradeEvent(BaseModel):
    id: str
    wallet_address: str
    market_id: str
    side: Literal["BUY", "SELL"]
    outcome: Literal["YES", "NO"]
    size_shares: int
    price_cents: int
    timestamp: datetime
    tx_hash: str | None

class Ingestor(Protocol):
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    def subscribe(self, queue: asyncio.Queue[TradeEvent]) -> None: ...
```

**Replay mode:** `scripts/replay.py --from 2025-11-01 --to 2026-01-31 --speed 100x` reads persisted trades from SQLite and re-emits them into the queue with accelerated timestamps. Essential for backtest and deterministic testing.

### 7.2 Wallet Profiler (`polysim.profiler`)

**Responsibilities**
- Maintain a rolling profile per wallet updated on every N trades or T minutes.
- Compute category distribution, Herfindahl exclusivity, win/loss record against resolved markets, funding-to-first-trade latency.
- Emit a `WalletProfileUpdated` event each update.

**Profile computation must be idempotent.** Re-running the profiler over the same data must yield identical output.

### 7.3 Scoring — Detectors

Each detector implements `Detector` and emits a `DetectorSignal` per (wallet, market) pair.

```python
class DetectorSignal(BaseModel):
    wallet_address: str
    market_id: str
    trade_id: str | None
    detector_name: str
    raw_score: float                  # 0..1
    confidence: float                 # 0..1, how sure the detector is of the score
    components: dict[str, float]      # interpretable sub-scores
    evidence: dict[str, Any]          # wallet age, size, etc. for debugging

class Detector(Protocol):
    name: str
    async def score(
        self,
        wallet: WalletProfile,
        market: Market,
        trade: TradeEvent | None,
    ) -> DetectorSignal | None: ...
```

**7.3.1 `CategoryInsiderDetector`**
- Binomial p-value of wallet's win rate within a single category, given category-level base rate (majority outcome).
- Requires ≥ N_MIN_RESOLVED resolved markets in category (config; default 8).
- Score = `1 - p_value`, capped at 0.999.
- Components: `category`, `wins`, `losses`, `p_value`, `base_rate`.

**7.3.2 `EventInsiderDetector`**
- Fires on a single trade, not a wallet history.
- Inputs: trade size, wallet nonce, market daily volume, side/outcome relative to current mid-price.
- Flags if all of: nonce < `FRESH_NONCE_THRESHOLD` (default 10), size > `FRESH_SIZE_MIN_CENTS` (default $5,000), market daily_volume_usd < `NICHE_MARKET_VOL_MAX` (default $500,000), trade is contrarian vs current mid by ≥ `CONTRARIAN_BPS` (default 15pts).
- Score = logistic combo of those factors.

**7.3.3 `FreshWalletDetector`**
- Softer version of 7.3.2 for general fresh-wallet monitoring without requiring contrarian bet.
- Used as a signal, not a standalone flag.

**7.3.4 `CoordinationDetector`**
- Graph of wallets co-occurring in the same low-volume market within a time window.
- Edge weight = co-occurrence count × (1 / market_volume).
- Flags clusters where any member has already been flagged by another detector.

**7.3.5 `TimingDetector`**
- % of wallet's position in a market opened in the last `LATE_WINDOW_HOURS` (default 1) before resolution.
- High concentration = timing signal. Score = that percentage, weighted by historical per-category base rate of late activity.

### 7.4 Composite Scorer (`polysim.scoring.composite`)

Takes all `DetectorSignal`s for a (wallet, market) pair and emits a single composite:

```python
class CompositeScore(BaseModel):
    wallet_address: str
    market_id: str
    score: float                      # 0..10
    components: dict[str, float]
    contributing_detectors: list[str]
    created_at: datetime
```

**Default weights (config-tunable):**

| Detector | Weight |
|---|---|
| CategoryInsiderDetector | 3.5 |
| EventInsiderDetector | 2.5 |
| TimingDetector | 1.5 |
| CoordinationDetector | 1.5 |
| FreshWalletDetector | 1.0 |

Composite = `sum(weight_i * raw_score_i * confidence_i)`, clamped to [0, 10].

Flag threshold default: composite ≥ 5.0 AND at least 2 detectors contributed non-zero scores.

### 7.5 Investigator Agent (`polysim.investigator`)

**Optional layer.** Runs only on flags above composite threshold. Uses Claude API.

```python
class InvestigatorVerdict(BaseModel):
    verdict: Literal["INFORMED", "LUCKY", "UNCLEAR"]
    confidence: float                 # 0..1
    reasoning: str                    # human-readable
    red_flags: list[str]
    green_flags: list[str]            # reasons to believe wallet is just skilled

class Investigator:
    async def investigate(
        self,
        flag: Flag,
        wallet_profile: WalletProfile,
        wallet_trade_history: list[TradeEvent],
        market: Market,
    ) -> InvestigatorVerdict: ...
```

**Prompt structure:** System prompt framed as "You are a prediction market integrity analyst. Given the following trading history, determine whether this wallet's pattern is more consistent with inside information or with skill/luck." User prompt includes wallet profile, last 50 trades, market metadata.

**Only act on flags with verdict `INFORMED` AND verdict_confidence ≥ 0.6.** Other flags are logged but not acted on.

**Cost guardrail:** max N_AGENT_CALLS_PER_DAY (default 100). When hit, new flags default-approve based on composite only.

### 7.6 Paper Executor (`polysim.paper`)

When a flag is approved (threshold passed + optional investigator), the paper executor simulates copy-trading:

1. Read current orderbook snapshot for the market (via Polymarket REST).
2. Determine copy size: `min(FIXED_COPY_CENTS, MAX_PCT_OF_BANKROLL * balance, MAX_PCT_OF_MARKET_VOL * market.daily_volume_usd)`.
3. Apply fill model (see §9) to compute fill price.
4. Write `paper_fills` row. Open or add to `paper_positions`.
5. On market resolution, close position at $1 or $0 and book realized P&L.

**Position management:**
- Max 1 open position per (run, market).
- Max K open positions per run (config; default 20).
- Per-wallet cap: aggregate notional exposure to positions sourced from one wallet ≤ `MAX_PCT_PER_SOURCE_WALLET` (default 10%).

### 7.7 Evaluator (`polysim.evaluator`)

Computes, on demand and nightly:

- P&L: realized, unrealized, total; per run, per category, per source wallet, per detector.
- Hit rate: % of closed positions with positive P&L.
- Sharpe (daily), max drawdown, return-per-flag.
- **Execution drag:** paper P&L vs an "oracle" counterfactual that fills at the exact insider's price and time. The difference is latency+slippage cost.
- **Detector attribution:** Shapley-style contribution of each detector to run P&L.
- Calibration: plot of composite score buckets vs realized hit rate.

All metrics are persisted and exposed via CLI.

### 7.8 Reporter

- `polysim dashboard` — rich TUI with live flag stream, open positions, P&L by category.
- `polysim report --run N` — full Markdown report for a run.
- Telegram: optional; push on flag, position open/close, daily summary.

---

## 8. Niches & category filters

Categories are tiered by expected edge, per operator ranking (§18, Q4).

- **Tier 1 — primary (real edge, enabled day 1).** `ai`, `aec`, `creator` are where the operator has identifiable domain advantage; the proven insider cases (§12 Phase 2 acceptance) all live here. `geopolitics`, `pop_culture`, `box_office` are also enabled as primary by operator interest, even though the insider-edge case for them is weaker — they're active to widen detector surface area.
- **Tier 2 — secondary (decent fit, noisier, enabled day 1 for calibration).** `macro_housing` is explicitly kept on as a sanity check: if the detector can't find alpha in a largely-public-data category, that itself is a signal about detector quality. `tech_ma` and `crypto_launches` enabled for opportunistic coverage.
- **Tier 3 — pilot (disabled; flip on when market volume appears).** `gov_defense` only.

```yaml
categories:
  # Tier 1 — primary, operator edge
  ai:
    enabled: true
    tier: primary
    keywords:
      - openai
      - anthropic
      - gpt
      - claude
      - gemini
      - llama
      - mistral
      - xai
      - meta ai
      - ai model
      - model launch
    examples:
      - "Will OpenAI release a new model by X?"
      - "Will Anthropic launch Y by Z?"
  aec:
    enabled: true
    tier: primary
    keywords:
      - autodesk
      - procore
      - bentley
      - trimble
      - hexagon
      - nemetschek
      - construction software
      - bim
    examples:
      - "Autodesk revenue above X in Q4?"
  creator:
    enabled: true
    tier: primary
    keywords:
      - mrbeast
      - youtube
      - tiktok
      - twitch
      - creator
      - streamer
    examples:
      - "MrBeast video X release date?"

  # Tier 1 — primary, operator interest (breadth)
  geopolitics:
    enabled: true
    tier: primary
    keywords:
      - election
      - geopolitics
      - ceasefire
      - sanctions
      - nato
      - un vote
      - referendum
  pop_culture:
    enabled: true
    tier: primary
    keywords:
      - grammy
      - oscar
      - emmy
      - album release
      - celebrity
      - chart
  box_office:
    enabled: true
    tier: primary
    keywords:
      - box office
      - opening weekend
      - film release
      - gross
      - theatrical

  # Tier 2 — secondary
  macro_housing:
    enabled: true
    tier: secondary
    keywords:
      - case-shiller
      - housing starts
      - housing permits
      - mortgage rate
      - fomc
      - fed
      - cpi
    note: "kept on deliberately as a detector-quality sanity check — mostly-public data"
  tech_ma:
    enabled: true
    tier: secondary
    keywords:
      - acquisition
      - merger
      - takeover
      - buyout
    note: "AEC-adjacent M&A overlaps; opportunistic only"
  crypto_launches:
    enabled: true
    tier: secondary
    keywords:
      - token unlock
      - tge
      - mainnet launch
      - airdrop

  # Tier 3 — pilot (off until volume warrants)
  gov_defense:
    enabled: false
    tier: pilot
    keywords:
      - dod contract
      - lockheed
      - raytheon
      - northrop
      - defense award
```

Classification is done by keyword match first (fast), then a bulk LLM classifier (Haiku) for anything unmatched, cached. The `tier` field is metadata, not a filter — it's surfaced in the reporter so per-tier P&L / flag rates are tracked separately.

---

## 9. Paper execution model

**The single biggest risk in this whole project is fooling yourself with an unrealistic fill model.** Pessimism is mandatory here.

### Fill model

For a copy-trade firing at time `t_flag` after the insider's trade at `t_insider`:

1. **Detection latency** `L_d`: time between insider's fill and our flag. Sampled from actual observed pipeline latency; default assumption 3–10 seconds.
2. **Decision latency** `L_x`: time from flag to paper execution. Includes investigator agent roundtrip if enabled. Default 2–15 seconds.
3. **Total latency** `L = L_d + L_x`.
4. **Price at execution:** query Polymarket for the orderbook at `t_flag + L` (or the closest snapshot). If not available, use linear interp between nearest snapshots with a pessimism bias (assume the worse of the two prices for us).
5. **Slippage:** walk the book for our size, adding `SLIPPAGE_TICKS` (default 1) penalty ticks beyond the book walk.
6. **Fee:** Polymarket maker/taker fees, currently 0 but configurable and logged.
7. **Partial fills:** if book depth at acceptable price < our intended size, take what's there and either (a) give up the rest (config `ON_PARTIAL: ABANDON`) or (b) walk further (`ON_PARTIAL: CHASE`, with a hard max 3 ticks).

### Calibration

The fill model has a `calibrate` command that runs a month of replay, compares paper fills to "perfect" fills (insider's price), and reports mean/p95 execution drag per category. Use this to parameterize realistic defaults.

### Resolution

On market resolution, open positions are closed at $1 (correct outcome) or $0 (wrong outcome) with no further fees.

**Invalid / disputed markets:** position is marked `RESOLVED` with `realized_pnl_cents = 0` and a warning emitted. These must be tracked separately in metrics — a strategy that looks good but relies on lots of invalid-market positions is suspect.

---

## 10. Metrics & evaluation

Required metrics per run:

- Total P&L, realized and unrealized, in cents
- Net return % vs starting balance
- Sharpe ratio (daily returns, annualized)
- Sortino ratio
- Max drawdown, duration, time to recover
- Win rate, avg win, avg loss, expectancy
- Trades per day, avg holding period
- P&L by category
- P&L by source wallet (top 10 and bottom 10)
- P&L by detector (attribution)
- Hit rate by composite score bucket (0.5-width)
- Execution drag: paper P&L minus oracle P&L, per category

Required for credibility:

- A baseline "null strategy" run that copy-trades *random* wallets at the same rate. If our strategy doesn't beat that baseline at p < 0.05, the run is a failure regardless of absolute P&L.
- A baseline "buy the favorite" run that always buys YES at the current mid for markets in our categories. Same p < 0.05 requirement.

---

## 11. Configuration

`config.yml` schema (example):

```yaml
run:
  name: "primary-test-2026-04"
  mode: "live_paper"                # or "backtest" or "replay"
  starting_balance_cents: 1000000   # $10,000 paper

bankroll:
  max_open_positions: 20
  max_pct_per_position: 0.02
  max_pct_per_source_wallet: 0.10
  max_pct_per_market: 0.05
  fixed_copy_cents: 5000            # $50 base, scaled by composite

scoring:
  flag_threshold: 5.0
  min_contributing_detectors: 2
  weights:
    CategoryInsiderDetector: 3.5
    EventInsiderDetector: 2.5
    TimingDetector: 1.5
    CoordinationDetector: 1.5
    FreshWalletDetector: 1.0

detectors:
  category_insider:
    min_resolved_markets: 8
  event_insider:
    fresh_nonce_threshold: 10
    fresh_size_min_cents: 500000
    niche_market_vol_max_cents: 50000000
    contrarian_bps: 1500

investigator:
  enabled: true
  model: "claude-opus-4-7"
  min_composite_to_invoke: 6.0
  min_verdict_confidence_to_act: 0.6
  max_calls_per_day: 100

fill_model:
  detection_latency_p50_ms: 3000
  detection_latency_p95_ms: 10000
  decision_latency_p50_ms: 2000
  decision_latency_p95_ms: 15000
  slippage_ticks: 1
  on_partial: "ABANDON"
  fee_bps: 0

categories:
  ai: { enabled: true, tier: primary }
  aec: { enabled: true, tier: primary }
  creator: { enabled: true, tier: primary }
  geopolitics: { enabled: true, tier: primary }
  pop_culture: { enabled: true, tier: primary }
  box_office: { enabled: true, tier: primary }
  macro_housing: { enabled: true, tier: secondary }
  tech_ma: { enabled: true, tier: secondary }
  crypto_launches: { enabled: true, tier: secondary }
  gov_defense: { enabled: false, tier: pilot }

# Advisory seed list — calibration only, not a scoring override.
# Addresses to be populated during Phase 2 backfill from the §13 backtest case set.
known_insiders:
  - { label: "AlphaRacoon (Google YIS)",       address: "" }
  - { label: "OpenAI browser flag (Oct 2025)", address: "" }
  - { label: "Venezuela cluster",              address: "" }
  - { label: "Maduro cluster",                 address: "" }
  - { label: "MrBeast producer",               address: "" }

telegram:
  enabled: true
  bot_token: "${TELEGRAM_BOT_TOKEN}"
  chat_id: "${TELEGRAM_CHAT_ID}"
```

`.env` contains `ANTHROPIC_API_KEY`, `ALCHEMY_API_KEY`, optional `TELEGRAM_*`. Never committed.

---

## 12. Build phases & acceptance criteria

### Phase 1 — Ingest & persist (target: ~1–2 working sessions)

- Ingestor streaming live Polymarket trades into SQLite.
- Markets table populated with category classification for at least the `ai` category.
- `polysim status` shows live trade count, recent markets, DB size.
- Tests: all ingest modules have unit tests with recorded HTTP fixtures.

**Accept when:** 24 hours of live ingest runs without crash; DB contains ≥ 10,000 trades and ≥ 50 classified markets.

### Phase 2 — Profiler & scoring (read-only, no paper trades yet)

- Wallet profiler running, wallet_profiles table populated.
- All detectors implemented with unit tests including adversarial / edge cases (empty history, single trade, perfectly winning wallet, etc.).
- Composite scorer producing flags written to `flags` table.
- CLI: `polysim flags --since 24h` lists recent flags.

**Accept when:** On a replay of the Oct 2025–Feb 2026 window, the system flags AlphaRacoon, the OpenAI browser wallet, and the Venezuela cluster before their respective resolutions, without >50 false positives per week.

### Phase 3 — Investigator

- Claude-powered investigator hooked in, writing verdicts to flags.
- Daily API call cap enforced.
- Per-flag cost logged.

**Accept when:** On the Phase 2 replay, the investigator correctly labels ≥ 70% of the known-insider flags as `INFORMED` and reduces false positive rate by ≥ 30%.

### Phase 4 — Paper executor & portfolio

- Paper executor opens and closes positions on flags.
- Fill model with calibrated defaults.
- `paper_runs`, `paper_positions`, `paper_fills` populated.
- CLI: `polysim run start`, `polysim run stop`, `polysim run status`.

**Accept when:** A 7-day backtest produces a full trade log with no negative balances, no positions exceeding caps, and a generated report.

### Phase 5 — Evaluator & reporter

- All metrics computed and dashboardable.
- Null-strategy and favorite-strategy baselines running alongside.
- Markdown report generator.

**Accept when:** Running a 90-day backtest produces a complete report with all §10 metrics, baseline comparisons, and a calibration plot.

### Phase 6 — Live paper mode

- Everything running in real time against live Polymarket data.
- Telegram alerts working.
- Daily summary emitted.

**Accept when:** 30 consecutive days of live paper trading with no crashes, ≥ 95% uptime, and a final report.

---

## 13. Testing plan

**Unit tests** (pytest)
- Every detector: edge cases (no history, all wins, all losses, single category, diverse categories)
- Fill model: walk-the-book logic, partial fill behavior, latency sampling
- Composite scorer: weight arithmetic, threshold logic
- Portfolio math: P&L, drawdown, Sharpe on synthetic series with known answers

**Property-based tests** (hypothesis)
- Portfolio invariants: sum of position P&L ≤ sum of fills; no position size < 0.
- Detector score monotonicity: more insider-like inputs → higher scores.

**Integration tests**
- Full pipeline on fixture data: replay 100 trades, assert expected flags.
- SQLite migrations forward and backward.

**Backtest acceptance**
- `tests/backtest/test_known_cases.py` encodes the known insider cases (AlphaRacoon, OpenAI browser, Venezuela cluster, Maduro cluster, MrBeast producer) and asserts the system flags each, with documented tolerance for timing.

**Null-result tests**
- A synthetic dataset of purely random wallets must yield composite scores with mean ≈ the score assigned to a "base rate" wallet, and negligible flag rate.

---

## 14. Hard rules (safety)

Violations of any of these are critical bugs that block merge:

1. **No order placement code path.** Grep CI check described in §4.
2. **No private keys anywhere.** Config file must not accept a private key field. Any wallet the user imports is address-only, view-only.
3. **Paper mode is the default and only trading mode.** There is no `mode: live_trading` option. The config schema must not accept such a value.
4. **Every paper run must log config snapshot at start.** So results are reproducible.
5. **Every flag must be reproducible from stored inputs.** Given `wallet_profile` at time T, `market` metadata, and `trade`, re-running the scorer must produce the identical flag. Non-deterministic detectors are disallowed.
6. **LLM outputs must never modify the detector behavior directly.** The investigator is a filter, not a scorer. It cannot change composite scores — only the `acted_on` boolean.
7. **Kill switches:**
   - Daily drawdown > 20% of starting balance → pause run, require manual resume.
   - >100 flags/hour → pause flagger, require manual investigation (likely a bug).
   - Any detector returning NaN or Inf → that detector is quarantined for the run.

---

## 15. Commands (CLI surface)

```
polysim init                            # create DB, config
polysim ingest start                    # begin live ingestion
polysim ingest backfill --days N
polysim profile rebuild
polysim flags list --since 24h
polysim flags show <flag_id>
polysim investigate <flag_id>           # manually invoke investigator
polysim run start --config config.yml
polysim run stop <run_id>
polysim run status <run_id>
polysim run list
polysim replay --from <date> --to <date> --speed 100x --config config.yml
polysim report --run <run_id> --format md > report.md
polysim dashboard                       # interactive TUI
polysim calibrate fill-model --days 30
```

---

## 16. The absolute prohibition

**This system must not place a real trade under any circumstance.**

It does not have, must not acquire, and must not be modifiable to acquire, the ability to sign or submit orders to any exchange. If during implementation any doubt arises about whether a code path could lead to real execution, the correct resolution is always to remove that code path.

The operator is aware of this constraint and agrees. The operator may separately, outside this system and with a clear air gap, choose to act on information they observe here. That is a decision made by a human, not an action taken by this software. The software's output is *information only*.

---

## 17. What to build first, literally

If you are Claude Code starting fresh:

1. Initialize `uv` project with the dependencies in §4.
2. Set up `pyproject.toml` with strict mypy, ruff.
3. Create the directory structure in §5.
4. Implement the SQLite schema and migrations (§6).
5. Implement `polysim init` and `polysim status` commands.
6. Stub all module files with `Protocol`/`ABC` signatures and `raise NotImplementedError`.
7. Write the test skeleton (§13) with xfail markers.
8. **Stop and show the operator.** Do not proceed to Phase 1 ingest until this skeleton is reviewed.

Subsequent phases follow §12 in order. Each phase ends with the operator reviewing the acceptance criteria before the next begins.

---

## 18. Operator decisions (answered 2026-04-19)

The Phase-4 pre-flight questions are now resolved. These answers are load-bearing for config defaults and §8 category expansion.

1. **Starting paper balance — $10,000.** Matches the spec default. `run.starting_balance_cents: 1000000` stands.
2. **Investigator activation — enable from Phase 2 onwards.** Operator's instruction was "whichever yields the most functionality", interpreted as turning the investigator on as soon as flags exist so the full pipeline is exercised earlier. The Phase 3 acceptance criterion (≥ 70 % `INFORMED` labelling on known cases, ≥ 30 % FP reduction) remains the validation gate — Phase 2 is allowed to *invoke* the investigator, but Phase 3 is where its lift is *measured* and signed off.
3. **Alerts — both Telegram and CLI.** `telegram.enabled: true` by default. CLI dashboard stays the primary interface; Telegram carries flag/open/close/daily-summary pushes.
4. **Categories beyond the three defaults — a full tiered expansion.** See the rewritten §8 below. Primary tier adds operator-interest niches (geopolitics, pop_culture, box_office) alongside the three edge niches (ai, aec, creator). Secondary tier activates macro_housing, tech_ma, crypto_launches (previously disabled). Pilot tier holds gov_defense off until volume warrants.
5. **Pre-seeded known-insider wallets — operator has none on hand; using the case set already embedded in §12/§13 acceptance tests as seed candidates.** Recommendation: pre-seed from the documented insider cases the system is already required to flag during backtest acceptance —
   - **AlphaRacoon** (Google Year in Search bet)
   - The **OpenAI browser flag** wallet (October 2025 case)
   - The **Venezuela cluster** (multi-wallet election trade)
   - The **Maduro cluster**
   - The **MrBeast producer** wallet
   Addresses are to be captured during the Phase 2 backfill and pinned into `config.known_insiders[]` (new config key). This list is advisory — calibration, not a scoring override.
6. **First live-paper run duration — 30 days.** As originally specced in Phase 6. No change.

---

---

*End of spec. Happy building.*
