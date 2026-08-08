"""Markdown report shape tests — build plan §5.7."""

from __future__ import annotations

from polysim.evaluator.metrics import RunMetrics
from polysim.evaluator.significance import TTestResult
from polysim.reporter.markdown import render


def _min_metrics() -> RunMetrics:
    return {
        "run_id": 1,
        "run_name": "test-run",
        "started_at": "2026-04-01T00:00:00+00:00",
        "ended_at": "2026-04-08T00:00:00+00:00",
        "window_days": 7.0,
        "starting_balance_cents": 1_000_000,
        "current_balance_cents": 1_050_000,
        "total_pnl_cents": 50_000,
        "realized_pnl_cents": 50_000,
        "unrealized_pnl_cents": 0,
        "net_return_pct": 0.05,
        "sharpe_annualized": 1.5,
        "sortino_annualized": 2.0,
        "max_drawdown_pct": -0.08,
        "max_drawdown_duration_days": 2,
        "max_drawdown_recovery_days": 3,
        "total_positions": 20,
        "closed_positions": 20,
        "open_positions": 0,
        "wins": 12,
        "losses": 8,
        "win_rate": 0.6,
        "avg_win_cents": 10_000,
        "avg_loss_cents": -5_000,
        "expectancy_cents": 4_000,
        "trades_per_day": 2.85,
        "avg_holding_hours": 48.0,
        "pnl_by_category": {"ai": 40_000, "aec": 10_000},
        "pnl_by_source_wallet_top": [
            {"wallet": "0xaf4d02c1b5e882f0", "pnl_cents": 15_000, "positions": 3},
        ],
        "pnl_by_source_wallet_bottom": [
            {"wallet": "0x0000000000000001", "pnl_cents": -8_000, "positions": 2},
        ],
        "pnl_by_detector": {"CategoryInsiderDetector": 20_000, "EventInsiderDetector": 30_000},
        "calibration_buckets": [
            {"range": "5.0-5.5", "n": 10, "hit_rate": 0.5},
            {"range": "7.0-7.5", "n": 5, "hit_rate": 0.8},
        ],
        "execution_drag_by_category": [
            {"category": "ai", "n": 15, "mean_drag_cents": 42.0,
             "p95_drag_cents": 100.0, "total_drag_cents": 630},
        ],
        "invalid_markets": 0,
        "invalid_market_pct": 0.0,
    }


def test_renders_all_required_sections() -> None:
    md = render(_min_metrics())
    # §10 sections + demo-panel headings
    for heading in (
        "# PolySim Run Report",
        "## 1. Headline metrics",
        "## 2. P&L by category",
        "## 3. Detector attribution",
        "## 5. Top source wallets",
        "## 6. Execution drag",
        "## 7. Calibration",
        "## 8. Invalid / disputed markets",
    ):
        assert heading in md, f"missing section: {heading}"


def test_includes_baseline_comparisons_when_provided() -> None:
    null_t = TTestResult(t_statistic=2.3, p_value=0.02, n_samples=20, alpha=0.05, passes=True)
    fav_t = TTestResult(t_statistic=1.1, p_value=0.15, n_samples=20, alpha=0.05, passes=False)
    md = render(_min_metrics(), null_test=null_t, favorite_test=fav_t)
    assert "## 4. Baseline comparisons" in md
    assert "0.0200" in md or "0.020" in md
    assert "PASS" in md
    assert "FAIL" in md


def test_invalid_market_warning_above_threshold() -> None:
    m = _min_metrics()
    m["invalid_markets"] = 5
    m["invalid_market_pct"] = 0.25
    md = render(m)
    assert "WARN" in md


def test_money_formatting() -> None:
    md = render(_min_metrics())
    # 50000 cents -> $500.00
    assert "$500.00" in md
    # -5000 avg loss -> -$50.00
    assert "-$50.00" in md


def test_kill_switch_notes_rendered() -> None:
    md = render(
        _min_metrics(),
        kill_switch_notes=["drawdown switch tripped 2026-04-05 12:00 UTC"],
    )
    assert "## 9. Kill-switch activity" in md
    assert "drawdown switch" in md
