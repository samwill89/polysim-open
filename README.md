# PolySim

Prediction-market insider detection and paper copy-trading simulator. See `polysim-spec.md` for the full spec, `polysim-buildplan.md` for the original execution plan, and `polysim-demo-annotated.html` for the annotated UI surface.

> This system never places real trades. See the safety guardrails below.

## Status

PolySim is now a **paper-only live research harness**, not a Phase 0 scaffold. The deployed app ingests Polymarket data, maintains wallet/market/trade state, and runs four clean v3 cohort-copy lanes with evidence, correlation, fee, and executable-edge controls. Historical variants remain queryable but are paused. The equity track runs an SMH benchmark and a slow-momentum shadow candidate. It still has no real-money path: order placement is absent, private keys are rejected by config, and `scripts/ci_safety.py` guards against adding trading APIs.

Current active honesty gaps:

- Alchemy wallet enrichment can hit sustained HTTP 429s; treat enrichment as degraded until the caller-level pause/circuit breaker is quiet in logs.
- No cohort-copy position in the current tournament window has resolved yet. Open-position marks are diagnostics, not proof of edge.
- External conversation signals have no live rows and their tournament variants are paused. They measure attention, not truth, and cannot authorize a paper trade. Catalyst-sensitive markets now fail closed without claim-specific corroboration and an explicit analyst probability.
- The equity walk-forward has no candidate that was positive in both deployed-data halves. `momentum_slow` is a forward paper test, not a promoted strategy.

## Setup

```bash
python -m pip install --user uv
uv python install 3.12
uv sync --extra dev
cp .env.example .env
cp config.example.yml config.yml
uv run polysim init
uv run polysim status
```

## Developer Workflow

```bash
uv run ruff check .
uv run ruff format .
uv run mypy src/polysim
uv run pytest
uv run python scripts/ci_safety.py
```

## Repo Layout

```text
polysim-spec.md              # spec
polysim-buildplan.md         # original task-level execution plan
polysim-demo.html            # one-page simulation of every UI surface
polysim-demo-annotated.html  # same, with plain-English explainers
config.example.yml           # copy to config.yml
.env.example                 # copy to .env

src/polysim/                 # library
  cli.py                     # typer entrypoint: `polysim`
  config.py                  # pydantic-settings + YAML
  models.py                  # shared pydantic models
  db/                        # SQLite schema, migrations, DAO
  ingest/                    # Polymarket WS/REST + Polygon RPC
  profiler/                  # wallet profiler
  scoring/                   # detectors + composite scorer
  investigator/              # Claude-based filter, not yet live in cohort-copy
  paper/                     # fill model + executor + portfolio
  risk_intelligence/         # evidence provenance, correlation, edge gates
  evaluator/                 # metrics, Shapley, baselines
  reporter/                  # TUI dashboard + Telegram
  utils/                     # logging, time helpers

tests/                       # unit, integration, backtest, golden, fixtures
scripts/ci_safety.py         # forbidden order-placement grep
docs/runbook.md              # operator handbook
docs/risk-intelligence.md    # evidence and pre-trade gate handbook
```

## Safety Guardrails

- **No order placement.** `scripts/ci_safety.py` greps for `place_order / create_order / cancel_order / post_order / sign_order` outside `tests/fixtures/` and fails the build.
- **No private keys.** The config schema rejects any `private_key` field.
- **Paper mode only.** `run.mode` accepts only `live_paper | backtest | replay`. There is no `live_trading` value.
- **Reproducible flags.** Every flag is reconstructible from stored inputs.

## Next Step

Keep the paper-only rails intact. Accumulate resolved outcomes for the four v3 betting lanes, compare the selective candidates against the controls after all recorded costs, and keep the equity candidate in shadow mode until it passes a fresh walk-forward and forward-paper gate.
