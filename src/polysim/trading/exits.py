"""Exit-trigger taxonomy — empirical-priors addendum §6.

Four triggers, each a strategy class implementing `should_exit`. The
trading loop runs every enabled trigger on every open position; the
first to fire wins. Counterfactual logging records what the OTHER
triggers would have done, so end-of-experiment can answer:
  - which trigger captured most of the P&L?
  - did the volume-spike trigger consistently get us out before drawdowns?
  - did the 85% target leave money on the table on big moves?

These are TESTABLE HYPOTHESES per §8. They ship enabled, but every
exit decision is logged with all four trigger verdicts.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from polysim.utils.time import iso, now_utc

log = logging.getLogger(__name__)

EXITS_LOG_DIR = Path("logs/exits")


@dataclass(frozen=True)
class ExitVerdict:
    """One trigger's read on a position."""

    trigger: str                          # 'target_hit' | 'volume_spike' | 'stale_thesis' | 'forecast_update'
    should_exit: bool
    reason: str = ""
    detail: dict[str, Any] | None = None


@dataclass(frozen=True)
class PositionState:
    """Minimal info each trigger needs."""

    position_id: int
    market_id: str
    outcome: str                          # YES / NO
    entry_price_cents: int
    current_bid_cents: int                # bid-priced mark per §4.1
    current_ask_cents: int
    expected_fair_value_cents: int        # what the investigator thinks YES is worth
    opened_at: datetime
    last_significant_price_change_at: datetime
    recent_10min_volume_cents: int
    avg_10min_volume_cents: int           # rolling baseline
    forecast_implied_probability: float | None = None  # for forecast-update trigger


# ── Trigger 1: Target hit ───────────────────────────────


@dataclass(frozen=True)
class TargetHitExit:
    """Exit at 85% of expected move toward fair value (Lunar §Step 4)."""

    target_pct: float = 0.85

    def evaluate(self, p: PositionState) -> ExitVerdict:
        side = p.outcome.upper()
        # YES position: profit accrues as bid → fair_value (>entry).
        if side == "YES":
            move_total = p.expected_fair_value_cents - p.entry_price_cents
            move_actual = p.current_bid_cents - p.entry_price_cents
        else:
            move_total = p.entry_price_cents - p.expected_fair_value_cents
            move_actual = p.entry_price_cents - p.current_bid_cents
        if move_total <= 0:
            return ExitVerdict(
                trigger="target_hit", should_exit=False,
                reason="no expected upward move",
            )
        captured_pct = max(0.0, move_actual / move_total)
        triggered = captured_pct >= self.target_pct
        return ExitVerdict(
            trigger="target_hit", should_exit=triggered,
            reason=(
                f"captured {captured_pct:.0%} of expected move "
                f"(target {self.target_pct:.0%})"
            ),
            detail={"captured_pct": captured_pct},
        )


# ── Trigger 2: Volume spike ─────────────────────────────


@dataclass(frozen=True)
class VolumeSpikeExit:
    """3x normal 10-min volume → 'smart money exiting' (Lunar §Step 4)."""

    multiplier: float = 3.0

    def evaluate(self, p: PositionState) -> ExitVerdict:
        if p.avg_10min_volume_cents <= 0:
            return ExitVerdict(
                trigger="volume_spike", should_exit=False,
                reason="no baseline volume",
            )
        ratio = p.recent_10min_volume_cents / p.avg_10min_volume_cents
        triggered = ratio >= self.multiplier
        return ExitVerdict(
            trigger="volume_spike", should_exit=triggered,
            reason=(
                f"10min volume {ratio:.1f}x baseline "
                f"(trigger >= {self.multiplier:.1f}x)"
            ),
            detail={"ratio": ratio},
        )


# ── Trigger 3: Stale thesis ─────────────────────────────


@dataclass(frozen=True)
class StaleThesisExit:
    """24h elapsed without significant price change → exit (Lunar §Step 4)."""

    stale_after: timedelta = timedelta(hours=24)

    def evaluate(self, p: PositionState, now: datetime | None = None) -> ExitVerdict:
        n = now or now_utc()
        elapsed = n - p.last_significant_price_change_at
        triggered = elapsed >= self.stale_after
        return ExitVerdict(
            trigger="stale_thesis", should_exit=triggered,
            reason=(
                f"{int(elapsed.total_seconds() / 3600)}h since significant move"
            ),
            detail={"hours_stale": elapsed.total_seconds() / 3600},
        )


# ── Trigger 4: Forecast update ──────────────────────────


@dataclass(frozen=True)
class ForecastUpdateExit:
    """Forecast shift against position before expiration (Opus 4.6 §strategy)."""

    threshold_pct: float = 0.10           # 10% drift triggers

    def evaluate(self, p: PositionState) -> ExitVerdict:
        if p.forecast_implied_probability is None:
            return ExitVerdict(
                trigger="forecast_update", should_exit=False,
                reason="no observable forecast",
            )
        # YES position is bullish on YES — forecast falling against us.
        side = p.outcome.upper()
        market_implied = p.current_ask_cents / 100.0  # rough mid proxy
        if side == "YES":
            shift = p.forecast_implied_probability - market_implied
        else:
            shift = market_implied - p.forecast_implied_probability
        triggered = shift <= -self.threshold_pct
        return ExitVerdict(
            trigger="forecast_update", should_exit=triggered,
            reason=(
                f"forecast vs market: {shift:+.0%} "
                f"(trigger <= {-self.threshold_pct:+.0%})"
            ),
            detail={"shift": shift},
        )


# ── Aggregate evaluator ─────────────────────────────────


@dataclass(frozen=True)
class EnabledTriggers:
    target_hit: bool = True
    volume_spike: bool = True
    stale_thesis: bool = True
    forecast_update: bool = True


def evaluate_all(
    p: PositionState, *, enabled: EnabledTriggers | None = None
) -> tuple[ExitVerdict | None, list[ExitVerdict]]:
    """Return (winner_verdict, all_verdicts).

    Counterfactual logging: callers persist `all_verdicts` so end-of-
    experiment analysis can answer 'what if trigger X were off?'
    """
    flags = enabled or EnabledTriggers()
    all_verdicts: list[ExitVerdict] = []
    if flags.target_hit:
        all_verdicts.append(TargetHitExit().evaluate(p))
    if flags.volume_spike:
        all_verdicts.append(VolumeSpikeExit().evaluate(p))
    if flags.stale_thesis:
        all_verdicts.append(StaleThesisExit().evaluate(p))
    if flags.forecast_update:
        all_verdicts.append(ForecastUpdateExit().evaluate(p))
    winner = next((v for v in all_verdicts if v.should_exit), None)
    return winner, all_verdicts


def append_exit_log(
    *,
    p: PositionState,
    winner: ExitVerdict | None,
    all_verdicts: Iterable[ExitVerdict],
) -> Path:
    EXITS_LOG_DIR.mkdir(parents=True, exist_ok=True)
    target = EXITS_LOG_DIR / f"{datetime.now(UTC).date().isoformat()}.jsonl"
    line = json.dumps({
        "logged_at": iso(now_utc()),
        "position_id": p.position_id,
        "market_id": p.market_id,
        "outcome": p.outcome,
        "winner": winner.trigger if winner else None,
        "verdicts": [
            {
                "trigger": v.trigger, "should_exit": v.should_exit,
                "reason": v.reason, "detail": v.detail,
            }
            for v in all_verdicts
        ],
    }, default=str) + "\n"
    with target.open("a", encoding="utf-8") as f:
        f.write(line)
    return target


__all__ = [
    "EnabledTriggers",
    "ExitVerdict",
    "ForecastUpdateExit",
    "PositionState",
    "StaleThesisExit",
    "TargetHitExit",
    "VolumeSpikeExit",
    "append_exit_log",
    "evaluate_all",
]
