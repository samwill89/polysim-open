# PolySim — Addendum 1: Dual-Mode Risk Profiles

**Version:** 0.1
**Status:** Extends the base spec at `polysim-spec.md`
**Audience:** Claude Code, implementing alongside or after base spec

---

## 0. Purpose

The base spec defines a single systematic, risk-managed paper-trading simulator. This addendum adds the ability to run **multiple concurrent paper runs with different risk profiles** against the same underlying flag stream, so the operator can empirically compare strategies over identical signal conditions.

Primary motivating question:

> *"If my signal detection is X, how do different risk management styles — from conservative-systematic to degen-YOLO — perform on the exact same flags over the same window?"*

This is an A/B/C test where the only variable is risk management. Signal detection, market data, and execution timing are held constant across profiles.

---

## 1. What changes vs. the base spec

**Added:**
- `RiskProfile` abstraction
- Three built-in profiles: `systematic`, `medium`, `degen`
- Ability to launch multiple concurrent `paper_runs` against one flag stream
- Comparative dashboard and report outputs
- Profile-aware market filtering inside the paper executor
- Profile-aware position sizing

**Unchanged:**
- The ingestor, profiler, scorer, investigator, and flag stream. These all operate identically regardless of how many paper runs are consuming their output.
- All safety invariants from base spec §14 and §16. Paper-only remains non-negotiable. Degen mode is *degenerate with paper money*, not with real money.

---

## 2. RiskProfile model

Add to `src/polysim/config.py`:

```python
from typing import Literal
from pydantic import BaseModel, Field

PositionSizingMode = Literal["fixed", "percentage", "kelly_fractional"]

class RiskProfile(BaseModel):
    name: str
    description: str

    # Position sizing
    position_sizing_mode: PositionSizingMode
    fixed_copy_cents: int | None = None              # used when mode == "fixed"
    max_pct_per_position: float                      # 0..1
    kelly_fraction: float | None = None              # used when mode == "kelly_fractional"

    # Portfolio caps
    max_open_positions: int
    max_pct_per_source_wallet: float                 # aggregate exposure to one insider
    max_pct_per_market: float                        # aggregate exposure to one market

    # Market filters
    min_market_odds: float | None = None             # e.g. 0.05 = only bet on <=5% implied prob
    max_market_odds: float | None = None             # e.g. 0.95 = only bet above 95% (rare)
    max_market_daily_volume_cents: int | None = None # thin-market filter
    min_market_daily_volume_cents: int | None = None # liquid-market filter

    # Signal filters
    allowed_detectors: list[str] | None = None       # None = all
    min_composite_score: float                       # profile-specific threshold

    # Risk management
    stop_loss_enabled: bool
    stop_loss_pct: float | None = None               # close position if price moves N% against
    drawdown_limit_pct: float | None = None          # pause run at this drawdown
    daily_loss_limit_pct: float | None = None        # pause run if day is this bad

    # Compounding behavior
    compound_winnings: bool                          # if True, sizing scales with balance
```

---

## 3. Built-in profiles

Persisted as YAML under `src/polysim/profiles/` and loaded at runtime. Users can add custom profiles to `~/.polysim/profiles/*.yml`.

### 3.1 `systematic.yml`

```yaml
name: systematic
description: >
  Conservative, diversified, risk-managed. Mirrors the base spec defaults.
  Designed to survive long enough to learn.
position_sizing_mode: fixed
fixed_copy_cents: 5000           # $50 per flag, regardless of balance
max_pct_per_position: 0.02       # hard cap 2% of bankroll
max_open_positions: 20
max_pct_per_source_wallet: 0.10
max_pct_per_market: 0.05
min_market_odds: null
max_market_odds: null
max_market_daily_volume_cents: null
min_market_daily_volume_cents: null
allowed_detectors: null          # all detectors allowed
min_composite_score: 5.0
stop_loss_enabled: true
stop_loss_pct: 0.50              # close if position value drops 50% pre-resolution
drawdown_limit_pct: 0.20
daily_loss_limit_pct: 0.05
compound_winnings: false
```

### 3.2 `medium.yml`

```yaml
name: medium
description: >
  Middle ground. Concentrated but not reckless. Kelly-sized with conservative
  fraction. Filters for slightly higher-odds markets.
position_sizing_mode: kelly_fractional
kelly_fraction: 0.25
max_pct_per_position: 0.10
max_open_positions: 8
max_pct_per_source_wallet: 0.25
max_pct_per_market: 0.15
min_market_odds: 0.10            # skip markets priced above 10% implied
max_market_odds: null
max_market_daily_volume_cents: 20000000   # $200k/day max (avoid most-liquid)
min_market_daily_volume_cents: 100000     # $1k/day min (avoid garbage)
allowed_detectors:
  - CategoryInsiderDetector
  - EventInsiderDetector
  - TimingDetector
min_composite_score: 6.0
stop_loss_enabled: true
stop_loss_pct: 0.70
drawdown_limit_pct: 0.40
daily_loss_limit_pct: 0.15
compound_winnings: true
```

### 3.3 `degen.yml`

```yaml
name: degen
description: >
  Mirrors the style of "100 to 100k in a month" accounts. Concentrated,
  uncapped, targeting thin markets at extreme odds. Most runs will blow up.
  The ones that don't, print.
position_sizing_mode: percentage
max_pct_per_position: 0.50       # HALF the bankroll per bet
max_open_positions: 3
max_pct_per_source_wallet: 1.00  # no cap
max_pct_per_market: 1.00         # no cap
min_market_odds: 0.01            # only 100:1+ shots...
max_market_odds: 0.15            # ...up to ~6:1
max_market_daily_volume_cents: 5000000    # $50k/day max (thin only)
min_market_daily_volume_cents: 10000      # $100/day min (avoid pure noise)
allowed_detectors:
  - EventInsiderDetector
  - FreshWalletDetector
min_composite_score: 4.0         # lower bar; volume of attempts matters
stop_loss_enabled: false
stop_loss_pct: null
drawdown_limit_pct: null         # YOLO
daily_loss_limit_pct: null
compound_winnings: true          # ride winners aggressively
```

---

## 4. Multi-run architecture

The base spec already supports multiple `paper_runs` rows. This addendum formalizes running them concurrently against one flag stream.

### 4.1 Flag distribution

The flag stream is a single `asyncio.Queue`. When a flag is emitted, a `Dispatcher` fans it out to all active runs. Each run's paper executor independently decides — based on its `RiskProfile` — whether to act on the flag.

```python
class Dispatcher:
    def __init__(self, runs: list[PaperRun]):
        self.runs = runs

    async def on_flag(self, flag: Flag) -> None:
        await asyncio.gather(*[
            run.executor.consider_flag(flag)
            for run in self.runs
            if run.status == "active"
        ])
```

**Important:** a single flag may result in zero, one, two, or three simulated positions — one per run that accepts it. Each run has its own independent balance and positions.

### 4.2 Database changes

No new tables. The existing `paper_runs`, `paper_positions`, and `paper_fills` tables already key off `run_id`, so multiple concurrent runs are naturally isolated.

Add one column to `paper_runs`:

```sql
ALTER TABLE paper_runs ADD COLUMN profile_name TEXT NOT NULL DEFAULT 'systematic';
ALTER TABLE paper_runs ADD COLUMN profile_snapshot_json TEXT NOT NULL DEFAULT '{}';
```

`profile_snapshot_json` stores the full RiskProfile at run start, so a profile edited mid-run doesn't retroactively change historical runs.

### 4.3 CLI

New and changed commands:

```bash
polysim profile list                                   # show built-in + user profiles
polysim profile show <name>                            # print profile yaml
polysim profile create <name> --from <existing>        # scaffold new profile
polysim profile edit <name>                            # open $EDITOR

polysim run start --profile systematic --balance 1000000 --name "sys-apr"
polysim run start --profile degen --balance 1000000 --name "degen-apr"
polysim run start --profile medium --balance 1000000 --name "med-apr"

# Launch all three at once
polysim run start-all --balance 1000000 --tag "apr-experiment"

polysim run list                                       # show all runs + status
polysim run compare --runs 1,2,3                       # side-by-side metrics
polysim run compare --tag apr-experiment               # all runs matching tag

polysim dashboard --split                              # split-pane TUI
polysim dashboard --run <id>                           # single-run TUI
```

### 4.4 Tags

Add optional `tag` field to `paper_runs` so a single A/B/C experiment is easily grouped and queried:

```sql
ALTER TABLE paper_runs ADD COLUMN tag TEXT;
CREATE INDEX idx_paper_runs_tag ON paper_runs(tag);
```

---

## 5. Paper executor changes

The paper executor (base spec §7.6) gets a RiskProfile reference and gates every decision on it.

```python
class PaperExecutor:
    def __init__(self, run: PaperRun, profile: RiskProfile, dao: DAO):
        self.run = run
        self.profile = profile
        self.dao = dao

    async def consider_flag(self, flag: Flag) -> None:
        # 1. Signal filters
        if self.profile.min_composite_score > flag.composite_score:
            return
        if self.profile.allowed_detectors is not None:
            contributing = set(flag.contributing_detectors)
            if not contributing.intersection(self.profile.allowed_detectors):
                return

        # 2. Market filters
        market = await self.dao.get_market(flag.market_id)
        current_odds = await self._get_current_mid(market)
        if self.profile.min_market_odds is not None:
            if current_odds > self.profile.min_market_odds:
                return
        if self.profile.max_market_odds is not None:
            if current_odds < self.profile.max_market_odds:
                return
        if self.profile.max_market_daily_volume_cents is not None:
            if market.daily_volume_cents > self.profile.max_market_daily_volume_cents:
                return
        if self.profile.min_market_daily_volume_cents is not None:
            if market.daily_volume_cents < self.profile.min_market_daily_volume_cents:
                return

        # 3. Portfolio cap checks (skip if already maxed out)
        if await self._at_max_positions():
            return
        if await self._exceeds_per_wallet_cap(flag.source_wallet):
            return
        if await self._exceeds_per_market_cap(market):
            return

        # 4. Risk gate
        if await self._drawdown_limit_tripped():
            return
        if await self._daily_loss_limit_tripped():
            return

        # 5. Size the position
        size_cents = await self._size_position(flag, market, current_odds)
        if size_cents <= 0:
            return

        # 6. Simulate fill and open
        await self._open_position(flag, market, size_cents)

    async def _size_position(self, flag: Flag, market: Market, odds: float) -> int:
        mode = self.profile.position_sizing_mode
        balance = await self.dao.get_run_balance(self.run.id)

        if mode == "fixed":
            base = self.profile.fixed_copy_cents or 0
        elif mode == "percentage":
            base = int(balance * self.profile.max_pct_per_position)
        elif mode == "kelly_fractional":
            edge = await self._estimate_edge(flag, odds)
            kelly = max(0.0, edge / (1 - odds) if odds < 1 else 0.0)
            frac = self.profile.kelly_fraction or 0.25
            base = int(balance * kelly * frac)
        else:
            base = 0

        # Always respect hard per-position cap
        hard_cap = int(balance * self.profile.max_pct_per_position)
        return min(base, hard_cap)
```

### 5.1 Stop-loss logic

For profiles with `stop_loss_enabled`, the paper executor also runs a periodic sweep (default every 60s) over open positions: if mark-to-market value has dropped by `stop_loss_pct` from entry, close the position at current mid with the normal fill model.

### 5.2 Drawdown / daily-loss pauses

If `drawdown_limit_pct` is breached, the run transitions to `paused` status. It does not resume automatically. Operator must `polysim run resume <id>` (a new command), which logs an acknowledgment. This exists specifically to *make the operator feel the loss* rather than auto-recover.

---

## 6. Comparative dashboard

New TUI view: `polysim dashboard --split` or `polysim dashboard --compare <tag>`.

```
┌─ SYSTEMATIC (Day 12 of 30) ────┬─ MEDIUM (Day 12 of 30) ───────┬─ DEGEN (Day 12 of 30) ────────┐
│ Balance:  $9,840  (-1.6%)      │ Balance:  $11,200 (+12.0%)    │ Balance:  $3,400  (-66.0%)    │
│ Peak:     $10,120              │ Peak:     $12,400             │ Peak:     $31,000             │
│ Drawdown: -3%                  │ Drawdown: -10%                │ Drawdown: -89%                │
│                                │                               │                               │
│ Trades:   37                   │ Trades:   11                  │ Trades:   4                   │
│ Win rate: 59%                  │ Win rate: 45%                 │ Win rate: 25%                 │
│ Avg win:  +$74                 │ Avg win:  +$820               │ Avg win:  +$21,000            │
│ Avg loss: -$102                │ Avg loss: -$450               │ Avg loss: -$9,400             │
│                                │                               │                               │
│ Sharpe:   -0.2                 │ Sharpe:   +1.1                │ Sharpe:   +0.3 (n too small)  │
│ Open:     11                   │ Open:     3                   │ Open:     1                   │
│                                │                               │                               │
│ Categories active:             │ Categories active:            │ Categories active:            │
│  ai      18 trades  +$120      │  ai      6 trades  +$1,400    │  ai      2 trades  +$21,000   │
│  aec      9 trades  -$180      │  aec     3 trades  -$100      │  creator 2 trades  -$18,800   │
│  creator 10 trades  -$100      │  creator 2 trades  -$100      │                               │
└────────────────────────────────┴───────────────────────────────┴───────────────────────────────┘

Flag stream (last 5, all runs see the same flags):
[14:02] Wallet 0x7a3... AI category, composite 6.8  →  SYS ✓  MED ✓  DEGEN ✗ (market too liquid)
[13:47] Wallet 0xfff... Event, composite 5.1        →  SYS ✓  MED ✗ (score)  DEGEN ✓
[13:31] Wallet 0x921... Coordination, composite 4.2 →  SYS ✗ (score)  MED ✗ (score)  DEGEN ✓
[13:18] Wallet 0x4b2... Category AI, composite 7.9  →  SYS ✓  MED ✓  DEGEN ✗ (odds too high)
[12:55] Wallet 0xdde... Event, composite 5.5        →  SYS ✓  MED ✗ (odds)  DEGEN ✓
```

The bottom panel is the most important piece: it shows, per flag, *which runs acted and why others didn't*. This is how you learn what each profile is actually doing.

---

## 7. Comparative report

`polysim report --tag <tag> --format md` produces a single combined report for an experiment:

- Headline table: starting balance, ending balance, return %, Sharpe, max drawdown per profile
- P&L curve: one chart, three lines (ascii or optional SVG)
- Flag acceptance breakdown: for each profile, how many flags were considered, accepted, and rejected — with rejection reasons counted
- Category breakdown per profile
- Per-wallet breakdown per profile (top 5 sources of P&L each way)
- **Key counterfactual:** for each profile, "what if this profile had used profile X's sizing on the same flag set" — a simple replay of the *other* profile's rules over this run's accepted flags. This isolates which part of the difference is sizing vs filtering.

---

## 8. Acceptance criteria for this addendum

Treat this as a "Phase 4.5" added to base spec §12.

**Accept when:**
1. `polysim profile list` shows systematic, medium, degen
2. `polysim run start-all --tag test` launches three concurrent runs that each receive every flag and make independent decisions
3. A 7-day backtest over replayed historical data produces three distinct P&L curves, all reproducible from the stored config snapshots
4. The comparative report correctly shows, for at least one flag in the log, at least one profile accepted it and at least one rejected it — with documented reason
5. Tests cover: profile loading, per-profile filter logic, per-profile sizing math, dispatcher fan-out, drawdown pause behavior

---

## 9. Operator protocol for the first 30-day experiment

This isn't an implementation requirement, it's a runbook entry. Add to `docs/runbook.md`:

1. Day 0: Start all three runs at $10,000 paper each with tag `dual-mode-experiment-01`. Record config snapshots. Do not touch the detectors or weights during the experiment — changing the signal mid-experiment invalidates the comparison.
2. Days 1-30: Check dashboard daily. Note any time a profile pauses due to drawdown limit. Do not resume paused runs during the experiment (a paused degen run is itself a data point).
3. Day 30: Generate comparative report. Answer:
   - Which profile had the best return? The best Sharpe?
   - If degen made money, was it from one lucky flag or distributed wins?
   - Which profile's *process* do you actually want to live with? (The systematic profile losing 3% in a month is a different psychological experience from the degen profile going $10k → $47k → $2k → $85k.)
   - What was execution drag per profile? (Thin markets = more drag.)
4. Based on findings, either (a) tweak a profile and run experiment 02, (b) settle on one profile, or (c) add a new profile that combines observed strengths.

---

## 10. What this addendum deliberately does not do

- **It does not add a live-trading mode.** Not for degen, not for any profile. Ever.
- **It does not let profiles modify detector behavior.** Detectors are identical across runs. Only risk management differs.
- **It does not allow real-time profile editing mid-run.** The profile is snapshotted at run start. To change it, start a new run.
- **It does not promise the degen profile can be profitable long-term.** The spec's position is that degen mode is expected-negative-EV with high variance, worth running only because (a) paper money is free and (b) empirical demonstration beats argument.

---

## 11. One explicit warning for the operator

The psychological trap of this architecture is clear: at some point during the 30-day experiment, the degen run will probably be up 300%+ and the systematic run will be grinding along at +5%. The instinct will be to "switch to degen with real money."

**That instinct is exactly the survivorship-bias trap this experiment is designed to reveal, not to confirm.** The relevant question after 30 days isn't "which profile is up more right now?" It's "given the distribution of possible 30-day outcomes for each profile, which one do you want to run for five years?" The degen profile's 30-day outcomes have a long right tail and a much heavier left tail. You're seeing one sample from each distribution, not the distributions themselves.

To help counteract this: the comparative report includes a **bootstrap simulation** that resamples the flag stream 1,000 times per profile and reports the distribution of 30-day outcomes. That's a more honest picture than any single run.

---

*End of addendum.*
