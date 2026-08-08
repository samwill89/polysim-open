"""Exit-trigger taxonomy tests — empirical-priors addendum §6."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from polysim.trading.exits import (
    EnabledTriggers,
    ForecastUpdateExit,
    PositionState,
    StaleThesisExit,
    TargetHitExit,
    VolumeSpikeExit,
    evaluate_all,
)


def _pos(
    *, entry: int = 30, bid: int = 35, ask: int = 36,
    fair: int = 50,
    opened_minutes_ago: int = 10,
    last_change_minutes_ago: int = 5,
    recent_vol: int = 1_000_00,
    avg_vol: int = 1_000_00,
    forecast: float | None = None,
    outcome: str = "YES",
) -> PositionState:
    now = datetime.now(UTC)
    return PositionState(
        position_id=1, market_id="m1", outcome=outcome,
        entry_price_cents=entry, current_bid_cents=bid,
        current_ask_cents=ask,
        expected_fair_value_cents=fair,
        opened_at=now - timedelta(minutes=opened_minutes_ago),
        last_significant_price_change_at=now - timedelta(minutes=last_change_minutes_ago),
        recent_10min_volume_cents=recent_vol,
        avg_10min_volume_cents=avg_vol,
        forecast_implied_probability=forecast,
    )


def test_target_hit_at_85pct_yes() -> None:
    # entry 30 → fair 50 (move 20). 85% target = 17 → bid >= 47.
    not_yet = TargetHitExit().evaluate(_pos(entry=30, bid=46, fair=50))
    triggered = TargetHitExit().evaluate(_pos(entry=30, bid=47, fair=50))
    assert not_yet.should_exit is False
    assert triggered.should_exit is True


def test_target_hit_no_position_inverts() -> None:
    # NO position: profit accrues as bid drops below entry toward fair_value.
    # entry 60 → fair 30 (down 30). 85% target = 25.5 → bid <= 34.5.
    not_yet = TargetHitExit().evaluate(_pos(entry=60, bid=35, fair=30, outcome="NO"))
    triggered = TargetHitExit().evaluate(_pos(entry=60, bid=33, fair=30, outcome="NO"))
    assert not_yet.should_exit is False
    assert triggered.should_exit is True


def test_volume_spike_3x_baseline() -> None:
    quiet = VolumeSpikeExit().evaluate(_pos(recent_vol=2_000_00, avg_vol=1_000_00))
    spike = VolumeSpikeExit().evaluate(_pos(recent_vol=3_500_00, avg_vol=1_000_00))
    assert quiet.should_exit is False
    assert spike.should_exit is True


def test_volume_spike_no_baseline_safe() -> None:
    v = VolumeSpikeExit().evaluate(_pos(recent_vol=10_000_00, avg_vol=0))
    assert v.should_exit is False


def test_stale_thesis_24h() -> None:
    fresh = StaleThesisExit().evaluate(_pos(last_change_minutes_ago=60))
    stale = StaleThesisExit().evaluate(_pos(last_change_minutes_ago=24 * 60 + 1))
    assert fresh.should_exit is False
    assert stale.should_exit is True


def test_forecast_update_drift_against_yes() -> None:
    # YES position, market_implied = ask/100 = 0.36, forecast = 0.20 → shift = -0.16 → trigger
    drift = ForecastUpdateExit().evaluate(_pos(ask=36, forecast=0.20))
    assert drift.should_exit is True
    # No drift case
    aligned = ForecastUpdateExit().evaluate(_pos(ask=36, forecast=0.40))
    assert aligned.should_exit is False


def test_forecast_no_observable_forecast_safe() -> None:
    v = ForecastUpdateExit().evaluate(_pos(forecast=None))
    assert v.should_exit is False


def test_evaluate_all_returns_winner_and_all() -> None:
    p = _pos(
        entry=30, bid=48, fair=50,                  # target captured
        recent_vol=4_000_00, avg_vol=1_000_00,      # also volume spike
    )
    winner, verdicts = evaluate_all(p)
    assert winner is not None
    assert winner.trigger in {"target_hit", "volume_spike"}
    # Counterfactual logging contract: ALL enabled triggers' verdicts are
    # included so we can ask "what if trigger X were off?"
    assert len(verdicts) == 4


def test_disabled_triggers_skipped() -> None:
    p = _pos(recent_vol=10_000_00, avg_vol=1_000_00)
    enabled = EnabledTriggers(volume_spike=False)
    _winner, verdicts = evaluate_all(p, enabled=enabled)
    assert all(v.trigger != "volume_spike" for v in verdicts)
    assert len(verdicts) == 3
