"""Pure metric helpers — synthetic series with known answers (build plan §5.2)."""

from __future__ import annotations

import math
from datetime import date, timedelta

from polysim.evaluator.metrics import (
    calibration_buckets,
    daily_returns_from_balance,
    execution_drag,
    max_drawdown,
    sharpe,
    sortino,
    win_stats,
)


class TestSharpe:
    def test_empty_series_is_zero(self) -> None:
        assert sharpe([]) == 0.0

    def test_constant_returns_is_zero(self) -> None:
        # stdev = 0 -> undefined -> we return 0.
        assert sharpe([0.01] * 20) == 0.0

    def test_alternating_returns_mean_zero(self) -> None:
        # mean 0, std positive -> Sharpe ~ 0.
        r = [0.01, -0.01] * 10
        assert abs(sharpe(r)) < 1e-6

    def test_positive_mean_positive_sharpe(self) -> None:
        # Mean 0.001, stdev 0.0005 → Sharpe ~ (0.001/0.0005) * sqrt(365) ≈ 38.2
        r = [0.001, 0.0015, 0.0005, 0.001, 0.002, 0.0]
        s = sharpe(r)
        assert s > 0
        # Known-answer check
        mean = sum(r) / len(r)
        import statistics as _s
        sd = _s.stdev(r)
        expected = (mean / sd) * math.sqrt(365)
        assert abs(s - expected) < 1e-6


class TestSortino:
    def test_no_downside_returns_zero(self) -> None:
        """All-positive returns → downside stdev is 0 → Sortino 0."""
        assert sortino([0.01, 0.02, 0.005]) == 0.0

    def test_downside_only(self) -> None:
        r = [0.01, -0.02, 0.01, -0.01, 0.005]
        s = sortino(r)
        assert s != 0


class TestMaxDrawdown:
    def test_monotonic_up(self) -> None:
        series = [(date(2026, 4, 1) + timedelta(days=i), 100 + i * 10) for i in range(5)]
        dd = max_drawdown(series)
        assert dd.max_drawdown_pct == 0.0
        assert dd.duration_days == 0
        assert dd.recovery_days is None

    def test_peak_trough_recovery(self) -> None:
        # 100, 110, 120, 80, 90, 130 → peak=120 at day 2, trough=80 at day 3,
        # recovery to >=120 at day 5 (value 130).
        vals = [100, 110, 120, 80, 90, 130]
        series = [
            (date(2026, 4, 1) + timedelta(days=i), v)
            for i, v in enumerate(vals)
        ]
        dd = max_drawdown(series)
        assert dd.max_drawdown_pct < 0.0
        # -40/120 = -0.3333...
        assert abs(dd.max_drawdown_pct + (40 / 120)) < 1e-9
        assert dd.duration_days == 1  # day 2 to day 3
        assert dd.recovery_days == 2  # day 3 to day 5

    def test_no_recovery(self) -> None:
        vals = [100, 110, 120, 80, 90, 100]  # never reaches 120 again
        series = [
            (date(2026, 4, 1) + timedelta(days=i), v)
            for i, v in enumerate(vals)
        ]
        dd = max_drawdown(series)
        assert dd.recovery_days is None


class TestWinStats:
    def test_all_wins(self) -> None:
        ws = win_stats([100, 50, 75])
        assert ws.wins == 3
        assert ws.losses == 0
        assert ws.win_rate == 1.0
        assert ws.avg_win_cents == 75
        assert ws.avg_loss_cents == 0

    def test_mixed(self) -> None:
        ws = win_stats([100, -50, 200, -100, 300])
        assert ws.wins == 3
        assert ws.losses == 2
        assert ws.win_rate == 0.6
        assert ws.avg_win_cents == 200
        assert ws.avg_loss_cents == -75
        # expectancy = 0.6 * 200 + 0.4 * -75 = 120 - 30 = 90
        assert ws.expectancy_cents == 90

    def test_zeros_excluded(self) -> None:
        ws = win_stats([0, 0, 100, -50])
        assert ws.wins == 1
        assert ws.losses == 1
        assert ws.win_rate == 0.5


class TestCalibrationBuckets:
    def test_bucket_boundaries(self) -> None:
        # Score 5.0 goes to 5.0-5.5 bucket; 5.5 goes to 5.5-6.0.
        data = [
            (5.0, 100),    # win, 5.0-5.5
            (5.1, -100),   # loss, 5.0-5.5
            (5.5, 100),    # win, 5.5-6.0
            (7.2, 100),    # win, 7.0-7.5
            (7.4, 100),    # win, 7.0-7.5
        ]
        buckets = calibration_buckets(data)
        by_range = {b["range"]: b for b in buckets}
        assert by_range["5.0-5.5"]["n"] == 2
        assert by_range["5.0-5.5"]["hit_rate"] == 0.5
        assert by_range["5.5-6.0"]["hit_rate"] == 1.0
        assert by_range["7.0-7.5"]["n"] == 2
        assert by_range["7.0-7.5"]["hit_rate"] == 1.0

    def test_empty(self) -> None:
        assert calibration_buckets([]) == []


class TestExecutionDrag:
    def test_groups_by_category(self) -> None:
        fills = [
            {"market_id": "m1", "slippage_cents": 2, "size_shares": 100,
             "position_id": 1, "fill_price_cents": 40},
            {"market_id": "m1", "slippage_cents": 3, "size_shares": 50,
             "position_id": 2, "fill_price_cents": 40},
            {"market_id": "m2", "slippage_cents": 1, "size_shares": 10,
             "position_id": 3, "fill_price_cents": 80},
        ]
        markets = {
            "m1": {"category": "ai"},
            "m2": {"category": "aec"},
        }
        out = execution_drag(fills, markets)
        by_cat = {d["category"]: d for d in out}
        assert by_cat["ai"]["n"] == 2
        # 2 * 100 + 3 * 50 = 200 + 150 = 350
        assert by_cat["ai"]["total_drag_cents"] == 350
        assert by_cat["aec"]["total_drag_cents"] == 10  # 1 * 10


class TestDailyReturns:
    def test_series_with_changes(self) -> None:
        series = [
            (date(2026, 4, 1), 100),
            (date(2026, 4, 2), 110),
            (date(2026, 4, 3), 100),
        ]
        returns = daily_returns_from_balance(series)
        # Day 1: (110-100)/100 = 0.1. Day 2: (100-110)/110 = -0.0909...
        assert len(returns) == 2
        assert abs(returns[0] - 0.1) < 1e-9
        assert abs(returns[1] + (10 / 110)) < 1e-9
