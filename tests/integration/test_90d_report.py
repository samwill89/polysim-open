"""90-day backtest acceptance — build plan Phase 5 acceptance.

Synthesizes 90 days of markets/flags/positions, runs both baselines,
computes metrics, renders the Markdown report, asserts every §10 section
is present and baseline p-values + calibration plot are rendered.
"""

from __future__ import annotations

import json
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from polysim.db import dao
from polysim.db.migrations.runner import apply_migrations
from polysim.evaluator import baselines
from polysim.evaluator.metrics import (
    _build_balance_series,
    _load_run_state,
    compute_run_metrics,
    daily_returns_from_balance,
)
from polysim.evaluator.significance import paired_t_test
from polysim.models import Flag, Market
from polysim.reporter.markdown import render

CATEGORIES = ["ai", "aec", "creator", "geopolitics", "pop_culture"]
DETECTORS_WEIGHTS = [
    ("CategoryInsiderDetector", 3.5),
    ("EventInsiderDetector", 2.5),
    ("TimingDetector", 1.5),
    ("CoordinationDetector", 1.5),
    ("FreshWalletDetector", 1.0),
]


async def _seed_90_day_run(db: Path, *, days: int = 90) -> int:
    """Build a realistic-ish primary run with spread positions + flags."""
    rng = random.Random(42)
    base = datetime(2025, 10, 1, tzinfo=UTC)

    run_id = await dao.create_paper_run(
        db,
        name="synthetic-90d",
        starting_balance_cents=1_000_000,
        config_snapshot={"synthetic": True},
    )

    # Seed N markets resolving uniformly through the window.
    n_markets = 40
    markets: list[tuple[str, str, str]] = []
    for i in range(n_markets):
        market_id = f"syn_m{i}"
        cat = rng.choice(CATEGORIES)
        # Biased toward YES so favorite-mid doesn't always win or lose.
        resolved = rng.choice(["YES", "NO", "YES", "YES", "NO"])
        resolves_at = base + timedelta(days=rng.randint(3, days - 3))
        await dao.upsert_market(
            db,
            Market(
                id=market_id,
                slug=market_id,
                question=f"Q-{market_id}?",
                category=cat,  # type: ignore[arg-type]
                created_at=base + timedelta(days=rng.randint(0, 2)),
                resolves_at=resolves_at,
                resolved_outcome=resolved,  # type: ignore[arg-type]
                resolved_at=resolves_at + timedelta(hours=1),
                daily_volume_usd_cents=rng.randint(100_000_00, 5_000_000_00),
            ),
        )
        markets.append((market_id, cat, resolved))

    # Seed ~120 flags + positions (some winners, some losers).
    wallets = [f"0xwallet{i:02d}" for i in range(12)]
    for w in wallets:
        await dao.upsert_wallet_first_sight(db, w)

    position_count = 0
    for i in range(120):
        (market_id, _cat, resolved) = rng.choice(markets)
        wallet = rng.choice(wallets)
        composite = rng.uniform(5.0, 9.5)

        # Random detector contributions summing to composite.
        weights_picked = rng.sample(DETECTORS_WEIGHTS, rng.randint(2, 4))
        w_sum = sum(w for _, w in weights_picked)
        contributions: dict[str, float] = {
            name: composite * (w / w_sum) for name, w in weights_picked
        }

        # Write flag.
        trade_id = f"syn_t{i}"
        flag_created = base + timedelta(
            days=rng.randint(0, days - 1),
            hours=rng.randint(0, 23),
            minutes=rng.randint(0, 59),
            seconds=i,  # ensures UNIQUE on created_at
        )
        flag_id = await dao.write_flag(
            db,
            Flag(
                wallet_address=wallet,
                market_id=market_id,
                trade_id=trade_id,
                detector_name="composite",
                raw_score=composite,
                composite_score=composite,
                components={
                    "contribution_by_detector": contributions,
                    "contributing_detectors": list(contributions.keys()),
                },
                created_at=flag_created,
            ),
        )
        if flag_id is None:
            continue

        # Pick direction: higher composite -> higher chance of matching resolved.
        if composite >= 7.5:
            outcome = resolved if rng.random() < 0.8 else ("NO" if resolved == "YES" else "YES")
        else:
            outcome = resolved if rng.random() < 0.55 else ("NO" if resolved == "YES" else "YES")

        size = rng.randint(50, 400)
        entry = rng.randint(25, 55)

        pos_id = await dao.write_paper_position(
            db, run_id=run_id, market_id=market_id, outcome=outcome,
            size_shares=size, avg_entry_price_cents=entry,
            source_flag_id=flag_id, source_wallet=wallet,
        )
        await dao.write_paper_fill(
            db, run_id=run_id, position_id=pos_id, side="BUY",
            size_shares=size, fill_price_cents=entry + rng.randint(0, 2),
            intended_price_cents=entry,
            slippage_cents=rng.randint(0, 2),
            latency_ms=rng.randint(3_000, 15_000),
            fee_cents=0,
        )
        await dao.adjust_run_balance(db, run_id, -(size * entry))

        # Close on resolution.
        payout = 100 if outcome == resolved else 0
        realized = size * (payout - entry)
        await dao.close_position(
            db, pos_id, realized_pnl_cents=realized, status="RESOLVED"
        )
        await dao.adjust_run_balance(db, run_id, size * payout)
        position_count += 1

    # End the run so `window_days` is well-defined.
    await dao.end_paper_run(db, run_id, notes=f"{position_count} positions")
    return run_id


@pytest.mark.integration
async def test_90d_backtest_produces_complete_report(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    await apply_migrations(db)
    run_id = await _seed_90_day_run(db)

    metrics = await compute_run_metrics(db, run_id)

    # Every required §10 key must be present.
    required = [
        "total_pnl_cents", "realized_pnl_cents", "unrealized_pnl_cents",
        "net_return_pct",
        "sharpe_annualized", "sortino_annualized",
        "max_drawdown_pct", "max_drawdown_duration_days",
        "win_rate", "avg_win_cents", "avg_loss_cents", "expectancy_cents",
        "trades_per_day", "avg_holding_hours",
        "pnl_by_category", "pnl_by_source_wallet_top",
        "pnl_by_source_wallet_bottom", "pnl_by_detector",
        "calibration_buckets", "execution_drag_by_category",
    ]
    for key in required:
        assert key in metrics, f"missing §10 metric: {key}"

    # Baseline runs + paired t-tests.
    null_id = await baselines.run_null_baseline(
        db, primary_run_id=run_id,
        starting_balance_cents=metrics["starting_balance_cents"], seed=0,
    )
    fav_id = await baselines.run_favorite_baseline(
        db, primary_run_id=run_id,
        starting_balance_cents=metrics["starting_balance_cents"],
    )

    primary_state = await _load_run_state(db, run_id)
    null_state = await _load_run_state(db, null_id)
    fav_state = await _load_run_state(db, fav_id)
    assert primary_state and null_state and fav_state

    p_ret = daily_returns_from_balance(_build_balance_series(primary_state))
    n_ret = daily_returns_from_balance(_build_balance_series(null_state))
    f_ret = daily_returns_from_balance(_build_balance_series(fav_state))
    null_test = paired_t_test(p_ret, n_ret)
    fav_test = paired_t_test(p_ret, f_ret)

    md = render(metrics, null_test=null_test, favorite_test=fav_test)

    # Every §10 section + baseline + calibration must be present.
    for heading in (
        "# PolySim Run Report",
        "## 1. Headline metrics",
        "## 2. P&L by category",
        "## 3. Detector attribution",
        "## 4. Baseline comparisons",
        "## 5. Top source wallets",
        "## 6. Execution drag",
        "## 7. Calibration",
        "## 8. Invalid / disputed markets",
    ):
        assert heading in md, f"missing section: {heading}"

    # p-values landed in the report.
    assert f"{null_test.p_value:.4f}" in md
    assert f"{fav_test.p_value:.4f}" in md

    # Calibration has at least one bucket.
    assert len(metrics["calibration_buckets"]) > 0, (
        "calibration empty — synthetic data didn't produce composite scores"
    )

    # Balance conservation: final current_balance must equal
    # starting + realized (since no open positions remain).
    final = int(metrics["current_balance_cents"])
    realized = int(metrics["realized_pnl_cents"])
    start = int(metrics["starting_balance_cents"])
    assert final == start + realized, (
        f"balance conservation broken: {final} != {start} + {realized}"
    )

    # Report length sanity.
    assert len(md) > 2_000, f"report too short ({len(md)} chars) — missing sections?"

    # Synthetic diagnostics we don't hard-assert on (too noisy):
    _ = metrics, md, json.dumps  # use json so lint doesn't complain
