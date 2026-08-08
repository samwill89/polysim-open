"""Run metrics — spec §10 + build plan §5.1 / §5.2 / §5.8.

Pure-function helpers (`sharpe`, `sortino`, `max_drawdown`, `win_stats`)
are exposed so tests can feed them synthetic series with known answers.
`compute_run_metrics(db_path, run_id)` assembles everything from SQLite.
"""

from __future__ import annotations

import json
import math
import statistics
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, TypedDict

from polysim.db import dao
from polysim.utils.time import parse_iso

TRADING_DAYS_PER_YEAR = 365  # prediction markets run weekends + holidays


@dataclass(frozen=True)
class DrawdownStats:
    max_drawdown_pct: float
    duration_days: int
    recovery_days: int | None      # None if not yet recovered


@dataclass(frozen=True)
class WinStats:
    wins: int
    losses: int
    win_rate: float
    avg_win_cents: int
    avg_loss_cents: int
    expectancy_cents: int


# ── RunMetrics shape ─────────────────────────────────────


class CalibrationBucket(TypedDict):
    range: str                  # "5.0-5.5"
    n: int
    hit_rate: float             # 0..1


class WalletPnl(TypedDict):
    wallet: str
    pnl_cents: int
    positions: int


class DragByCategory(TypedDict):
    category: str
    n: int
    mean_drag_cents: float
    p95_drag_cents: float
    total_drag_cents: int


class RunMetrics(TypedDict, total=False):
    # identity
    run_id: int
    run_name: str
    started_at: str
    ended_at: str | None
    window_days: float

    # headline
    starting_balance_cents: int
    current_balance_cents: int
    total_pnl_cents: int
    realized_pnl_cents: int
    unrealized_pnl_cents: int
    net_return_pct: float

    # risk-adjusted
    sharpe_annualized: float
    sortino_annualized: float
    max_drawdown_pct: float
    max_drawdown_duration_days: int
    max_drawdown_recovery_days: int | None

    # position stats
    total_positions: int
    closed_positions: int
    open_positions: int
    wins: int
    losses: int
    win_rate: float
    avg_win_cents: int
    avg_loss_cents: int
    expectancy_cents: int
    trades_per_day: float
    avg_holding_hours: float

    # breakdowns
    pnl_by_category: dict[str, int]
    pnl_by_source_wallet_top: list[WalletPnl]
    pnl_by_source_wallet_bottom: list[WalletPnl]
    pnl_by_detector: dict[str, int]
    calibration_buckets: list[CalibrationBucket]
    execution_drag_by_category: list[DragByCategory]

    # hygiene
    invalid_markets: int
    invalid_market_pct: float


# ── pure helpers ─────────────────────────────────────────


def sharpe(
    returns: Sequence[float],
    *,
    risk_free: float = 0.0,
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
) -> float:
    """Annualized Sharpe ratio. Returns 0.0 when stdev == 0 or n < 2."""
    if len(returns) < 2:
        return 0.0
    excess = [r - risk_free for r in returns]
    mean = statistics.mean(excess)
    sd = statistics.stdev(excess)
    if sd == 0.0:
        return 0.0
    return (mean / sd) * math.sqrt(periods_per_year)


def sortino(
    returns: Sequence[float],
    *,
    risk_free: float = 0.0,
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
) -> float:
    """Annualized Sortino ratio (downside-stdev denominator)."""
    if len(returns) < 2:
        return 0.0
    excess = [r - risk_free for r in returns]
    mean = statistics.mean(excess)
    downside = [min(0.0, e) for e in excess]
    if all(d == 0.0 for d in downside):
        return 0.0
    down_var = sum(d * d for d in downside) / len(downside)
    down_sd = math.sqrt(down_var)
    if down_sd == 0.0:
        return 0.0
    return (mean / down_sd) * math.sqrt(periods_per_year)


def max_drawdown(balance_series: Sequence[tuple[date, int]]) -> DrawdownStats:
    """Max peak-to-trough drawdown on a dated balance series.

    Returns (max_dd_pct, duration_days, recovery_days_or_None).
    Duration is days from peak to trough. Recovery is days from trough to
    first time balance returns to that peak (None if never).
    """
    if len(balance_series) < 2:
        return DrawdownStats(0.0, 0, None)

    peak_balance = balance_series[0][1]
    peak_date = balance_series[0][0]
    worst_dd = 0.0
    worst_peak_date = peak_date
    worst_trough_date = peak_date

    for d, bal in balance_series:
        if bal > peak_balance:
            peak_balance = bal
            peak_date = d
        dd = (bal - peak_balance) / peak_balance if peak_balance != 0 else 0.0
        if dd < worst_dd:
            worst_dd = dd
            worst_trough_date = d
            worst_peak_date = peak_date

    if worst_dd == 0.0:
        return DrawdownStats(0.0, 0, None)

    duration_days = (worst_trough_date - worst_peak_date).days

    # Find first date after trough where balance returns to peak.
    recovery: int | None = None
    reached_peak = False
    # Recompute peak value at the drawdown time.
    # (Walk again to get peak-value-at-trough-time.)
    peak_at_trough = 0
    running_peak = balance_series[0][1]
    for d, bal in balance_series:
        if bal > running_peak:
            running_peak = bal
        if d == worst_trough_date:
            peak_at_trough = running_peak
            break

    for d, bal in balance_series:
        if d <= worst_trough_date:
            continue
        if bal >= peak_at_trough:
            recovery = (d - worst_trough_date).days
            reached_peak = True
            break

    return DrawdownStats(
        max_drawdown_pct=float(worst_dd),
        duration_days=duration_days,
        recovery_days=recovery if reached_peak else None,
    )


def win_stats(realized_pnls: Sequence[int]) -> WinStats:
    """Win rate / avg win / avg loss / expectancy from realized cents."""
    wins = [p for p in realized_pnls if p > 0]
    losses = [p for p in realized_pnls if p < 0]
    n_total = len(wins) + len(losses)
    win_rate = len(wins) / n_total if n_total else 0.0
    avg_win = int(statistics.mean(wins)) if wins else 0
    avg_loss = int(statistics.mean(losses)) if losses else 0
    expectancy = (
        int(win_rate * avg_win + (1 - win_rate) * avg_loss) if n_total else 0
    )
    return WinStats(
        wins=len(wins),
        losses=len(losses),
        win_rate=win_rate,
        avg_win_cents=avg_win,
        avg_loss_cents=avg_loss,
        expectancy_cents=expectancy,
    )


def calibration_buckets(
    flag_scores_and_pnls: Sequence[tuple[float, int]],
    *,
    bin_width: float = 0.5,
    floor_score: float = 5.0,
) -> list[CalibrationBucket]:
    """Return ordered buckets covering [floor, 10.0] with hit_rate per bin."""
    buckets: dict[float, list[int]] = defaultdict(list)
    for score, pnl in flag_scores_and_pnls:
        lo = math.floor(max(floor_score, score) / bin_width) * bin_width
        buckets[lo].append(pnl)
    out: list[CalibrationBucket] = []
    for lo in sorted(buckets.keys()):
        pnls = buckets[lo]
        if not pnls:
            continue
        hit_rate = sum(1 for p in pnls if p > 0) / len(pnls)
        out.append({
            "range": f"{lo:.1f}-{lo + bin_width:.1f}",
            "n": len(pnls),
            "hit_rate": hit_rate,
        })
    return out


def execution_drag(
    fills: Sequence[dict[str, Any]],
    markets_by_id: dict[str, dict[str, Any]],
) -> list[DragByCategory]:
    """Mean/p95 slippage per category — Build plan §5.8."""
    by_cat: dict[str, list[int]] = defaultdict(list)
    for fill in fills:
        slip = int(fill.get("slippage_cents") or 0)
        # Drag is positive when we paid more than intended (BUY) or
        # received less than intended (SELL). slippage_cents already
        # reflects that semantics in the fill model.
        size = int(fill.get("size_shares") or 0)
        drag_cents = slip * size
        # Need the market category — positions table linkage via position_id
        # would be heavier than the fill schema; look up via market_id.
        position_id = fill.get("position_id")
        _ = position_id
        # fills don't directly carry market_id; fall back on all fills getting
        # bucketed by the position's market. Here we assume market_id is
        # preloaded on the fill dict by the caller.
        market_id = str(fill.get("market_id") or "")
        cat = "unknown"
        if market_id and market_id in markets_by_id:
            cat = str(markets_by_id[market_id].get("category") or "unknown")
        by_cat[cat].append(drag_cents)

    out: list[DragByCategory] = []
    for cat, drags in sorted(by_cat.items(), key=lambda kv: -sum(kv[1])):
        if not drags:
            continue
        mean_drag = statistics.mean(drags)
        p95 = _percentile(drags, 0.95)
        out.append({
            "category": cat,
            "n": len(drags),
            "mean_drag_cents": float(mean_drag),
            "p95_drag_cents": float(p95),
            "total_drag_cents": int(sum(drags)),
        })
    return out


def _percentile(xs: Sequence[float], q: float) -> float:
    if not xs:
        return 0.0
    ordered = sorted(xs)
    k = max(0, min(len(ordered) - 1, round(q * (len(ordered) - 1))))
    return float(ordered[k])


def daily_returns_from_balance(
    balance_series: Sequence[tuple[date, int]],
) -> list[float]:
    """Convert an EOD-balance series to simple daily returns."""
    if len(balance_series) < 2:
        return []
    out: list[float] = []
    prev_bal = balance_series[0][1]
    for _, bal in balance_series[1:]:
        if prev_bal <= 0:
            out.append(0.0)
        else:
            out.append((bal - prev_bal) / prev_bal)
        prev_bal = bal
    return out


# ── DB orchestration ─────────────────────────────────────


@dataclass(frozen=True)
class _RunState:
    run: dict[str, Any]
    positions: list[dict[str, Any]]
    fills: list[dict[str, Any]]
    markets_by_id: dict[str, dict[str, Any]]
    flags_by_id: dict[int, dict[str, Any]]


async def _load_run_state(db_path: Path, run_id: int) -> _RunState | None:
    run = await dao.get_paper_run(db_path, run_id)
    if run is None:
        return None

    positions_open = await dao.list_open_positions(db_path, run_id)
    positions_all = positions_open + await _list_closed_positions(db_path, run_id)
    fills = await dao.list_paper_fills(db_path, run_id)

    # Fetch markets + flags referenced by these positions.
    market_ids = {str(p.get("market_id")) for p in positions_all}
    markets_by_id: dict[str, dict[str, Any]] = {}
    for mid in market_ids:
        m = await dao.get_market(db_path, mid)
        if m is not None:
            markets_by_id[mid] = m.model_dump()

    flag_ids = {int(p.get("source_flag_id") or 0) for p in positions_all}
    flag_ids.discard(0)
    flags_by_id: dict[int, dict[str, Any]] = {}
    for fid in flag_ids:
        f = await dao.get_flag(db_path, fid)
        if f is not None:
            flags_by_id[fid] = f

    # Annotate fills with their position's market_id (for drag bucketing).
    pos_to_market = {int(p["id"]): str(p["market_id"]) for p in positions_all}
    for fill in fills:
        pid = int(fill.get("position_id") or 0)
        fill["market_id"] = pos_to_market.get(pid, "")

    return _RunState(
        run=run,
        positions=positions_all,
        fills=fills,
        markets_by_id=markets_by_id,
        flags_by_id=flags_by_id,
    )


async def _list_closed_positions(
    db_path: Path, run_id: int
) -> list[dict[str, Any]]:
    import aiosqlite

    if not db_path.exists():
        return []
    try:
        async with aiosqlite.connect(str(db_path)) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM paper_positions "
                "WHERE run_id = ? AND status IN ('CLOSED', 'RESOLVED') "
                "ORDER BY opened_at",
                (run_id,),
            ) as cur:
                rows = await cur.fetchall()
    except aiosqlite.OperationalError:
        return []
    return [dict(r) for r in rows]


def _build_balance_series(
    state: _RunState,
) -> list[tuple[date, int]]:
    """Walk fills + closes chronologically and build EOD balances."""
    starting = int(state.run.get("starting_balance_cents") or 0)
    started_at = (
        parse_iso(str(state.run["started_at"]))
        if state.run.get("started_at")
        else datetime.now(UTC)
    )
    ended_at = (
        parse_iso(str(state.run["ended_at"]))
        if state.run.get("ended_at")
        else datetime.now(UTC)
    )

    # Each event is (timestamp, delta_cents).
    events: list[tuple[datetime, int]] = []
    for fill in state.fills:
        size = int(fill.get("size_shares") or 0)
        price = int(fill.get("fill_price_cents") or 0)
        fee = int(fill.get("fee_cents") or 0)
        side = str(fill.get("side"))
        ts = parse_iso(str(fill.get("timestamp") or started_at.isoformat()))
        # BUY: balance -= cost + fee. SELL: balance += proceeds - fee.
        if side == "BUY":
            events.append((ts, -(size * price + fee)))
        else:
            events.append((ts, size * price - fee))

    for pos in state.positions:
        if pos.get("status") not in {"CLOSED", "RESOLVED"}:
            continue
        closed_at = pos.get("closed_at")
        if not closed_at:
            continue
        ts = parse_iso(str(closed_at))
        pnl = int(pos.get("realized_pnl_cents") or 0)
        size = int(pos.get("size_shares") or 0)
        entry = int(pos.get("avg_entry_price_cents") or 0)
        # Resolution credits (entry_cost + realized_pnl) back to balance.
        events.append((ts, size * entry + pnl))

    events.sort(key=lambda ev: ev[0])

    # Aggregate per date.
    start_date = started_at.date()
    end_date = ended_at.date()
    # Ensure we have at least start + end dates.
    if end_date < start_date:
        end_date = start_date
    days = (end_date - start_date).days
    series: list[tuple[date, int]] = []
    running = starting
    ev_idx = 0

    for offset in range(days + 1):
        d = start_date + timedelta(days=offset)
        cutoff = datetime.combine(d, datetime.min.time()).replace(tzinfo=UTC) + timedelta(days=1)
        while ev_idx < len(events) and events[ev_idx][0] < cutoff:
            running += events[ev_idx][1]
            ev_idx += 1
        series.append((d, running))
    return series


def _attribute_pnl_by_detector(
    state: _RunState,
) -> dict[str, int]:
    """Proportional attribution — shapley.py delegates here if it wants."""
    per_detector: dict[str, int] = defaultdict(int)
    for pos in state.positions:
        fid = pos.get("source_flag_id")
        if fid is None:
            continue
        flag = state.flags_by_id.get(int(fid))
        if flag is None:
            continue
        components_raw = flag.get("components_json")
        if not components_raw:
            continue
        try:
            components = json.loads(components_raw)
        except (json.JSONDecodeError, TypeError):
            continue
        contrib = components.get("contribution_by_detector") or {}
        if not isinstance(contrib, dict) or not contrib:
            continue
        total = sum(
            float(v) for v in contrib.values() if isinstance(v, (int, float))
        )
        if total <= 0:
            continue
        pnl = int(pos.get("realized_pnl_cents") or 0)
        for det, c in contrib.items():
            try:
                share = int(pnl * (float(c) / total))
            except (TypeError, ValueError):
                continue
            per_detector[det] += share
    return dict(per_detector)


async def compute_run_metrics(
    db_path: Path, run_id: int
) -> RunMetrics:
    state = await _load_run_state(db_path, run_id)
    if state is None:
        return RunMetrics(run_id=run_id, run_name="unknown")

    run = state.run
    starting = int(run.get("starting_balance_cents") or 0)
    current = int(run.get("current_balance_cents") or 0)

    # P&L breakdowns.
    closed = [p for p in state.positions if p.get("status") in {"CLOSED", "RESOLVED"}]
    open_positions = [p for p in state.positions if p.get("status") == "OPEN"]
    realized = sum(int(p.get("realized_pnl_cents") or 0) for p in closed)

    # Unrealized — mark-to-BID, per empirical-priors addendum §4.1.
    # Mid-price MTM systematically overstates performance by the spread
    # (2-5c on Polymarket), so bid is the only sanctioned mark for open
    # positions. Fall back to entry-price mark (0 net) when no orderbook
    # snapshot is available for a position.
    from polysim.portfolio.valuation import (
        best_bid_ask_from_snapshot,
        value_position,
    )
    unrealized_cost = sum(
        int(p.get("size_shares") or 0) * int(p.get("avg_entry_price_cents") or 0)
        for p in open_positions
    )
    unrealized = 0
    for pos in open_positions:
        mid_str = str(pos.get("market_id") or "")
        outcome = str(pos.get("outcome") or "")
        if not mid_str or not outcome:
            continue
        quote = await best_bid_ask_from_snapshot(
            db_path, market_id=mid_str, outcome=outcome,
        )
        if quote is None:
            continue
        best_bid, best_ask = quote
        val = value_position(
            position_id=int(pos.get("id") or 0),
            size_shares=int(pos.get("size_shares") or 0),
            avg_entry_price_cents=int(pos.get("avg_entry_price_cents") or 0),
            bid_price_cents=best_bid,
            ask_price_cents=best_ask,
        )
        unrealized += val.unrealized_pnl_cents

    total_pnl = realized + unrealized
    net_return = (total_pnl / starting) if starting > 0 else 0.0

    # Daily-return series for Sharpe/Sortino/DD.
    balance_series = _build_balance_series(state)
    returns = daily_returns_from_balance(balance_series)
    dd = max_drawdown(balance_series)
    sharpe_val = sharpe(returns)
    sortino_val = sortino(returns)

    # Position stats.
    pnl_list = [int(p.get("realized_pnl_cents") or 0) for p in closed]
    wstats = win_stats(pnl_list)
    started_at = parse_iso(str(run["started_at"])) if run.get("started_at") else datetime.now(UTC)
    ended_at = parse_iso(str(run["ended_at"])) if run.get("ended_at") else datetime.now(UTC)
    window_days = max(1.0, (ended_at - started_at).total_seconds() / 86400.0)
    trades_per_day = len(state.positions) / window_days
    holding_hours = [
        (parse_iso(str(p["closed_at"])) - parse_iso(str(p["opened_at"]))).total_seconds() / 3600.0
        for p in closed
        if p.get("opened_at") and p.get("closed_at")
    ]
    avg_holding = statistics.mean(holding_hours) if holding_hours else 0.0

    # Category breakdown.
    pnl_by_category: dict[str, int] = defaultdict(int)
    for pos in closed:
        mid = str(pos.get("market_id") or "")
        cat = str(
            (state.markets_by_id.get(mid) or {}).get("category") or "unknown"
        )
        pnl_by_category[cat] += int(pos.get("realized_pnl_cents") or 0)

    # Wallet breakdown.
    by_wallet: dict[str, list[int]] = defaultdict(list)
    for pos in closed:
        w = str(pos.get("source_wallet") or "")
        if not w:
            continue
        by_wallet[w].append(int(pos.get("realized_pnl_cents") or 0))
    wallet_pnls: list[WalletPnl] = [
        {"wallet": w, "pnl_cents": sum(ps), "positions": len(ps)}
        for w, ps in by_wallet.items()
    ]
    wallet_pnls.sort(key=lambda x: -x["pnl_cents"])
    top_wallets = wallet_pnls[:10]
    bottom_wallets = sorted(wallet_pnls, key=lambda x: x["pnl_cents"])[:10]

    # Detector attribution.
    by_detector = _attribute_pnl_by_detector(state)

    # Calibration (composite score bucket → hit rate).
    flag_pnls: list[tuple[float, int]] = []
    for pos in closed:
        fid = pos.get("source_flag_id")
        if fid is None:
            continue
        flag = state.flags_by_id.get(int(fid))
        if flag is None:
            continue
        score = flag.get("composite_score")
        if score is None:
            continue
        flag_pnls.append((float(score), int(pos.get("realized_pnl_cents") or 0)))
    calibration = calibration_buckets(flag_pnls)

    # Execution drag.
    drag = execution_drag(state.fills, state.markets_by_id)

    # Invalid markets.
    invalid_closed = [
        p for p in closed
        if (state.markets_by_id.get(str(p.get("market_id"))) or {}).get("resolved_outcome")
        == "INVALID"
    ]
    invalid_markets = len(invalid_closed)
    invalid_pct = invalid_markets / len(closed) if closed else 0.0

    metrics: RunMetrics = {
        "run_id": int(run["id"]),
        "run_name": str(run.get("name") or ""),
        "started_at": str(run.get("started_at") or ""),
        "ended_at": str(run["ended_at"]) if run.get("ended_at") else None,
        "window_days": window_days,
        "starting_balance_cents": starting,
        "current_balance_cents": current,
        "total_pnl_cents": total_pnl,
        "realized_pnl_cents": realized,
        "unrealized_pnl_cents": unrealized,
        "net_return_pct": net_return,
        "sharpe_annualized": sharpe_val,
        "sortino_annualized": sortino_val,
        "max_drawdown_pct": dd.max_drawdown_pct,
        "max_drawdown_duration_days": dd.duration_days,
        "max_drawdown_recovery_days": dd.recovery_days,
        "total_positions": len(state.positions),
        "closed_positions": len(closed),
        "open_positions": len(open_positions),
        "wins": wstats.wins,
        "losses": wstats.losses,
        "win_rate": wstats.win_rate,
        "avg_win_cents": wstats.avg_win_cents,
        "avg_loss_cents": wstats.avg_loss_cents,
        "expectancy_cents": wstats.expectancy_cents,
        "trades_per_day": trades_per_day,
        "avg_holding_hours": avg_holding,
        "pnl_by_category": dict(pnl_by_category),
        "pnl_by_source_wallet_top": top_wallets,
        "pnl_by_source_wallet_bottom": bottom_wallets,
        "pnl_by_detector": by_detector,
        "calibration_buckets": calibration,
        "execution_drag_by_category": drag,
        "invalid_markets": invalid_markets,
        "invalid_market_pct": invalid_pct,
    }
    _ = unrealized_cost  # reserved for Phase 6 live-mark unrealized calc
    return metrics
