"""Read-only, point-in-time strategy analysis for the live PolySim database.

The older ad-hoc backtests in this repository are useful diagnostics, but they
are not portfolio simulations: some reuse current cohort membership across old
history, count repeated trades in the same market, or credit resolution
proceeds before the market actually resolves.  This script is deliberately
stricter:

* cohort signals are evaluated only after the first live tournament started;
* one paper position is allowed per market;
* cash remains locked until a recorded resolution timestamp;
* open positions are marked to the latest observed trade at the cutoff;
* train and validation windows are evaluated at their own historical cutoffs;
* strategy grids are ranked by validation return, then sample size.

It uses only the Python standard library so it can be uploaded to a Fly
machine and run directly against ``/data/polysim.db``.
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import statistics
from collections import defaultdict, deque
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

STARTING_BALANCE_CENTS = 1_000_000
FIXED_STAKE_CENTS = 5_000
MAX_OPEN_POSITIONS = 20
SLIPPAGE_CENTS = 1


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


def pct(numerator: int | float, denominator: int | float) -> float:
    return float(numerator) / float(denominator) if denominator else 0.0


def category_name(value: Any) -> str:
    text = str(value or "unknown").strip().lower()
    return text or "unknown"


@dataclass(frozen=True)
class SignalEvent:
    market_id: str
    outcome: str
    timestamp: datetime
    source_wallet: str
    entry_price_cents: int
    source_notional_cents: int
    confirming_wallets: int
    category: str
    daily_volume_cents: int
    resolved_outcome: str | None
    resolved_at: datetime | None


@dataclass(frozen=True)
class StrategySpec:
    confirmations: int
    min_notional_cents: int
    max_entry_cents: int
    categories: tuple[str, ...] = ()
    min_market_volume_cents: int = 0

    @property
    def name(self) -> str:
        cats = "+".join(self.categories) if self.categories else "all"
        liquidity = self.min_market_volume_cents / 100
        return (
            f"confirm{self.confirmations}_min${self.min_notional_cents / 100:g}"
            f"_max{self.max_entry_cents}c_{cats}_liq${liquidity:g}"
        )


@dataclass
class SimulationResult:
    strategy: str
    start: str
    end: str
    ending_account_cents: int
    return_pct: float
    bets_opened: int
    bets_resolved: int
    wins: int
    losses: int
    open_positions: int
    skipped_capacity: int
    skipped_filter: int
    realized_pnl_cents: int
    unrealized_pnl_cents: int
    win_rate: float
    profit_factor: float | None


def connect_read_only(path: Path) -> sqlite3.Connection:
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    conn.execute("PRAGMA temp_store = MEMORY")
    return conn


class MarketMarks:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self._cache: dict[tuple[str, str, str], int | None] = {}

    def latest_at(
        self,
        market_id: str,
        outcome: str,
        cutoff: datetime,
        fallback: int,
    ) -> int:
        day_key = cutoff.strftime("%Y-%m-%dT%H:%M:%S")
        key = (market_id, outcome, day_key)
        if key not in self._cache:
            row = self.conn.execute(
                "SELECT price_cents FROM trades "
                "WHERE market_id = ? AND outcome = ? AND timestamp <= ? "
                "ORDER BY timestamp DESC LIMIT 1",
                (market_id, outcome, iso(cutoff)),
            ).fetchone()
            self._cache[key] = int(row[0]) if row and row[0] is not None else None
        return int(self._cache[key] if self._cache[key] is not None else fallback)


def database_vintage(conn: sqlite3.Connection) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for table in ("trades", "wallets", "markets", "flags", "paper_runs"):
        row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        counts[table] = int(row[0]) if row else 0
    latest_trade = conn.execute("SELECT MAX(timestamp) FROM trades").fetchone()
    latest_flag = conn.execute("SELECT MAX(created_at) FROM flags").fetchone()
    signal_counts: dict[str, int] = {}
    for table in ("conversation_snapshots", "market_signals"):
        try:
            row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        except sqlite3.OperationalError:
            row = None
        signal_counts[table] = int(row[0]) if row else 0
    return {
        "counts": counts,
        "latest_trade": str(latest_trade[0] or "") if latest_trade else "",
        "latest_flag": str(latest_flag[0] or "") if latest_flag else "",
        "external_signal_rows": signal_counts,
    }


def _last_trade_marks_for_open_positions(
    conn: sqlite3.Connection,
    rows: Iterable[sqlite3.Row],
) -> tuple[int, int, int]:
    value = 0
    marked = 0
    entry_fallback = 0
    cache: dict[tuple[str, str], int | None] = {}
    for row in rows:
        market_id = str(row["market_id"])
        outcome = str(row["outcome"])
        key = (market_id, outcome)
        if key not in cache:
            mark_row = conn.execute(
                "SELECT price_cents FROM trades "
                "WHERE market_id = ? AND outcome = ? "
                "ORDER BY timestamp DESC LIMIT 1",
                key,
            ).fetchone()
            cache[key] = int(mark_row[0]) if mark_row and mark_row[0] is not None else None
        entry = int(row["avg_entry_price_cents"] or 0)
        if cache[key] is None:
            mark = entry
            entry_fallback += 1
        else:
            mark = int(cache[key])
            marked += 1
        value += int(row["size_shares"] or 0) * mark
    return value, marked, entry_fallback


def run_scorecards(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    runs = conn.execute("SELECT * FROM paper_runs WHERE ended_at IS NULL ORDER BY id").fetchall()
    out: list[dict[str, Any]] = []
    for run in runs:
        run_id = int(run["id"])
        start = int(run["starting_balance_cents"] or 0)
        balance = int(run["current_balance_cents"] or 0)
        tag = str(run["tag"] or "")
        if tag == "equity_v1":
            positions = conn.execute(
                "SELECT * FROM equity_positions WHERE run_id = ?",
                (run_id,),
            ).fetchall()
            open_positions = [p for p in positions if str(p["status"]) == "OPEN"]
            closed = [p for p in positions if str(p["status"]) == "CLOSED"]
            pnls = [int(p["realized_pnl_cents"] or 0) for p in closed]
            cash_row = conn.execute(
                "SELECT cash_cents FROM equity_run_state WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            account = balance
            mark_source = "stored_equity"
            cash = int(cash_row[0]) if cash_row else None
            marked = len(open_positions)
            fallback = 0
            uncredited_exit = 0
            exit_breakdown = [
                {
                    "reason": str(row[0] or "unknown"),
                    "positions": int(row[1]),
                    "realized_pnl_cents": int(row[2] or 0),
                }
                for row in conn.execute(
                    "SELECT exit_reason, COUNT(*), "
                    "COALESCE(SUM(realized_pnl_cents), 0) "
                    "FROM equity_positions WHERE run_id = ? AND status = 'CLOSED' "
                    "GROUP BY exit_reason ORDER BY COUNT(*) DESC",
                    (run_id,),
                ).fetchall()
            ]
            ticker_rows = [
                {
                    "ticker": str(row[0]),
                    "positions": int(row[1]),
                    "realized_pnl_cents": int(row[2] or 0),
                }
                for row in conn.execute(
                    "SELECT ticker, COUNT(*), "
                    "COALESCE(SUM(realized_pnl_cents), 0) "
                    "FROM equity_positions WHERE run_id = ? AND status = 'CLOSED' "
                    "GROUP BY ticker",
                    (run_id,),
                ).fetchall()
            ]
            ticker_rows.sort(key=lambda row: int(row["realized_pnl_cents"]))
        else:
            positions = conn.execute(
                "SELECT * FROM paper_positions WHERE run_id = ?",
                (run_id,),
            ).fetchall()
            open_positions = [p for p in positions if str(p["status"]) == "OPEN"]
            closed = [p for p in positions if str(p["status"]) != "OPEN"]
            pnls = [int(p["realized_pnl_cents"] or 0) for p in closed]
            open_value, marked, fallback = _last_trade_marks_for_open_positions(
                conn, open_positions
            )
            account = balance + open_value
            mark_source = "latest_trade_or_entry"
            cash = balance
            uncredited_row = conn.execute(
                "SELECT COALESCE(SUM("
                "p.size_shares * p.avg_entry_price_cents "
                "+ p.realized_pnl_cents), 0) "
                "FROM paper_positions p "
                "JOIN paper_runs pr ON pr.id = p.run_id "
                "JOIN flags f ON f.id = p.source_flag_id "
                "WHERE p.run_id = ? AND p.status = 'CLOSED' "
                "AND pr.tag = 'tournament_v1' "
                "AND f.detector_name = 'CohortCopy' "
                "AND p.realized_pnl_cents IS NOT NULL "
                "AND NOT EXISTS (SELECT 1 FROM paper_fills f "
                "WHERE f.position_id = p.id AND f.side = 'SELL') "
                "AND EXISTS (SELECT 1 FROM trades t "
                "WHERE LOWER(t.wallet_address) = LOWER(p.source_wallet) "
                "AND t.market_id = p.market_id AND t.outcome = p.outcome "
                "AND t.side = 'SELL' "
                "AND t.price_cents = CAST((p.size_shares * "
                "p.avg_entry_price_cents + p.realized_pnl_cents) "
                "/ p.size_shares AS INTEGER) "
                "AND t.timestamp >= p.opened_at "
                "AND t.timestamp <= p.closed_at)",
                (run_id,),
            ).fetchone()
            uncredited_exit = int(uncredited_row[0]) if uncredited_row else 0
            exit_breakdown = []
            ticker_rows = []

        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        gross_win = sum(wins)
        gross_loss = -sum(losses)
        fees_row = conn.execute(
            "SELECT COALESCE(SUM(fee_cents), 0), "
            "COALESCE(SUM(ABS(slippage_cents) * size_shares), 0) "
            "FROM paper_fills WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        out.append(
            {
                "run_id": run_id,
                "name": str(run["name"]),
                "tag": tag,
                "started_at": str(run["started_at"]),
                "paused": bool(run["paused_at"]),
                "pause_reason": run["pause_reason"],
                "starting_balance_cents": start,
                "cash_cents": cash,
                "account_value_cents": account,
                "return_pct": pct(account - start, start),
                "uncredited_mirror_exit_cents": uncredited_exit,
                "corrected_account_value_cents": account + uncredited_exit,
                "corrected_return_pct": pct(account + uncredited_exit - start, start),
                "total_positions": len(positions),
                "closed_positions": len(closed),
                "open_positions": len(open_positions),
                "realized_pnl_cents": sum(pnls),
                "win_rate": pct(len(wins), len(wins) + len(losses)),
                "avg_win_cents": statistics.mean(wins) if wins else 0.0,
                "avg_loss_cents": statistics.mean(losses) if losses else 0.0,
                "profit_factor": (gross_win / gross_loss) if gross_loss else None,
                "mark_source": mark_source,
                "marked_positions": marked,
                "entry_fallback_positions": fallback,
                "fees_cents": int(fees_row[0]) if fees_row else 0,
                "slippage_drag_cents": int(fees_row[1]) if fees_row else 0,
                "exit_breakdown": exit_breakdown,
                "pnl_by_ticker_bottom": ticker_rows[:10],
                "pnl_by_ticker_top": list(reversed(ticker_rows[-10:])),
            }
        )
    return out


def tournament_start(conn: sqlite3.Connection) -> datetime:
    row = conn.execute(
        "SELECT MIN(started_at) FROM paper_runs WHERE tag = 'tournament_v1'"
    ).fetchone()
    parsed = parse_dt(str(row[0])) if row and row[0] else None
    if parsed is not None:
        return parsed
    latest = conn.execute("SELECT MIN(timestamp) FROM trades").fetchone()
    return parse_dt(str(latest[0])) or datetime.now(UTC) - timedelta(days=60)


def equity_data_vintage(conn: sqlite3.Connection) -> dict[str, Any]:
    try:
        quote = conn.execute(
            "SELECT MIN(date), MAX(date), COUNT(*), COUNT(DISTINCT ticker) FROM equity_quotes"
        ).fetchone()
        sentiment = conn.execute(
            "SELECT MIN(ts), MAX(ts), COUNT(*), COUNT(DISTINCT ticker) FROM equity_sentiment"
        ).fetchone()
    except sqlite3.OperationalError:
        return {}
    return {
        "quotes": {
            "first": str(quote[0] or ""),
            "last": str(quote[1] or ""),
            "rows": int(quote[2] or 0),
            "tickers": int(quote[3] or 0),
        },
        "sentiment": {
            "first": str(sentiment[0] or ""),
            "last": str(sentiment[1] or ""),
            "rows": int(sentiment[2] or 0),
            "tickers": int(sentiment[3] or 0),
        },
    }


def load_confirmation_events(
    conn: sqlite3.Connection,
    *,
    start: datetime,
    max_confirmations: int = 3,
    confirmation_window: timedelta = timedelta(hours=24),
) -> dict[int, list[SignalEvent]]:
    """First point at which N distinct wallets agree on market and side."""
    rows = conn.execute(
        "SELECT f.id AS flag_id, f.created_at, "
        "t.timestamp, t.wallet_address, t.market_id, t.outcome, "
        "t.price_cents, t.size_shares, "
        "m.category, m.daily_volume_usd_cents, "
        "m.resolved_outcome, m.resolved_at "
        "FROM flags f "
        "JOIN trades t ON t.id = f.trade_id "
        "JOIN markets m ON m.id = t.market_id "
        "WHERE f.detector_name = 'CohortCopy' "
        "AND t.side = 'BUY' AND t.timestamp >= ? "
        "ORDER BY t.timestamp ASC, f.id ASC",
        (iso(start),),
    ).fetchall()

    windows: dict[tuple[str, str], deque[tuple[datetime, str]]] = defaultdict(deque)
    emitted: dict[int, set[str]] = {n: set() for n in range(1, max_confirmations + 1)}
    events: dict[int, list[SignalEvent]] = {n: [] for n in range(1, max_confirmations + 1)}
    for row in rows:
        ts = parse_dt(str(row["timestamp"]))
        if ts is None:
            continue
        market_id = str(row["market_id"])
        outcome = str(row["outcome"]).upper()
        key = (market_id, outcome)
        q = windows[key]
        cutoff = ts - confirmation_window
        while q and q[0][0] < cutoff:
            q.popleft()
        wallet = str(row["wallet_address"] or "").lower()
        q.append((ts, wallet))
        distinct = len({w for _, w in q if w})
        for n in range(1, max_confirmations + 1):
            if distinct < n or market_id in emitted[n]:
                continue
            emitted[n].add(market_id)
            events[n].append(
                SignalEvent(
                    market_id=market_id,
                    outcome=outcome,
                    timestamp=ts,
                    source_wallet=wallet,
                    entry_price_cents=int(row["price_cents"] or 0),
                    source_notional_cents=(
                        int(row["price_cents"] or 0) * int(row["size_shares"] or 0)
                    ),
                    confirming_wallets=distinct,
                    category=category_name(row["category"]),
                    daily_volume_cents=int(row["daily_volume_usd_cents"] or 0),
                    resolved_outcome=(
                        str(row["resolved_outcome"]).upper()
                        if row["resolved_outcome"] in ("YES", "NO")
                        else None
                    ),
                    resolved_at=parse_dt(row["resolved_at"]),
                )
            )
    return events


@dataclass
class _OpenBet:
    event: SignalEvent
    shares: int
    entry: int
    cost: int


def simulate(
    events: list[SignalEvent],
    spec: StrategySpec,
    *,
    start: datetime,
    end: datetime,
    marks: MarketMarks,
) -> SimulationResult:
    cash = STARTING_BALANCE_CENTS
    opened = 0
    skipped_capacity = 0
    skipped_filter = 0
    realized_pnl = 0
    wins = 0
    losses = 0
    gross_wins = 0
    gross_losses = 0
    active: dict[str, _OpenBet] = {}

    def release(cutoff: datetime) -> None:
        nonlocal cash, realized_pnl, wins, losses, gross_wins, gross_losses
        done: list[str] = []
        for market_id, bet in active.items():
            ev = bet.event
            if (
                ev.resolved_at is None
                or ev.resolved_at > cutoff
                or ev.resolved_outcome not in ("YES", "NO")
            ):
                continue
            payout = 100 if ev.outcome == ev.resolved_outcome else 0
            proceeds = bet.shares * payout
            pnl = proceeds - bet.cost
            cash += proceeds
            realized_pnl += pnl
            if pnl > 0:
                wins += 1
                gross_wins += pnl
            elif pnl < 0:
                losses += 1
                gross_losses -= pnl
            done.append(market_id)
        for market_id in done:
            del active[market_id]

    allowed_categories = set(spec.categories)
    for ev in events:
        if ev.timestamp < start or ev.timestamp >= end:
            continue
        release(ev.timestamp)
        passes = (
            0 < ev.entry_price_cents <= spec.max_entry_cents
            and ev.source_notional_cents >= spec.min_notional_cents
            and ev.daily_volume_cents >= spec.min_market_volume_cents
            and (not allowed_categories or ev.category in allowed_categories)
        )
        if not passes:
            skipped_filter += 1
            continue
        if ev.market_id in active:
            continue
        if len(active) >= MAX_OPEN_POSITIONS:
            skipped_capacity += 1
            continue
        entry = min(99, ev.entry_price_cents + SLIPPAGE_CENTS)
        shares = FIXED_STAKE_CENTS // entry if entry > 0 else 0
        cost = shares * entry
        if shares <= 0 or cost > cash:
            skipped_capacity += 1
            continue
        cash -= cost
        active[ev.market_id] = _OpenBet(ev, shares, entry, cost)
        opened += 1

    release(end)
    open_value = 0
    for bet in active.values():
        mark = marks.latest_at(
            bet.event.market_id,
            bet.event.outcome,
            end,
            bet.entry,
        )
        open_value += bet.shares * mark
    unrealized = open_value - sum(b.cost for b in active.values())
    account = cash + open_value
    return SimulationResult(
        strategy=spec.name,
        start=iso(start),
        end=iso(end),
        ending_account_cents=account,
        return_pct=pct(account - STARTING_BALANCE_CENTS, STARTING_BALANCE_CENTS),
        bets_opened=opened,
        bets_resolved=wins + losses,
        wins=wins,
        losses=losses,
        open_positions=len(active),
        skipped_capacity=skipped_capacity,
        skipped_filter=skipped_filter,
        realized_pnl_cents=realized_pnl,
        unrealized_pnl_cents=unrealized,
        win_rate=pct(wins, wins + losses),
        profit_factor=(gross_wins / gross_losses) if gross_losses else None,
    )


def strategy_grid() -> list[StrategySpec]:
    categories = (
        (),
        ("politics", "sports"),
        ("sports",),
        ("politics",),
    )
    return [
        StrategySpec(
            confirmations=confirmations,
            min_notional_cents=notional,
            max_entry_cents=max_entry,
            categories=cats,
            min_market_volume_cents=min_volume,
        )
        for confirmations in (1, 2, 3)
        for notional in (1_000, 10_000, 50_000, 100_000)
        for max_entry in (70, 80, 90)
        for cats in categories
        for min_volume in (0, 2_000_000, 5_000_000)
    ]


def selected_strategy_names() -> set[str]:
    """Production candidates plus their no-liquidity-filter comparisons."""
    specs = [
        StrategySpec(1, 1_000, 70, (), 5_000_000),
        StrategySpec(1, 1_000, 70, (), 2_000_000),
        StrategySpec(1, 10_000, 70),
        StrategySpec(1, 1_000, 70, ("politics", "sports"), 5_000_000),
        StrategySpec(1, 1_000, 70, ("politics", "sports"), 2_000_000),
        StrategySpec(1, 1_000, 70, ("politics", "sports")),
    ]
    return {spec.name for spec in specs}


def bucket_rows(events: list[SignalEvent], cutoff: datetime) -> dict[str, Any]:
    """Resolved-only descriptive outcomes, never used as a portfolio return."""
    rows = [
        e
        for e in events
        if e.resolved_at is not None
        and e.resolved_at <= cutoff
        and e.resolved_outcome in ("YES", "NO")
        and 0 < e.entry_price_cents < 100
    ]

    def summarize(groups: dict[str, list[SignalEvent]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for key, vals in groups.items():
            rois: list[float] = []
            for event in vals:
                won = event.outcome == event.resolved_outcome
                rois.append(
                    ((100 - event.entry_price_cents) / event.entry_price_cents) if won else -1.0
                )
            out.append(
                {
                    "bucket": key,
                    "n": len(vals),
                    "win_rate": pct(
                        sum(1 for e in vals if e.outcome == e.resolved_outcome), len(vals)
                    ),
                    "mean_roi": statistics.mean(rois) if rois else 0.0,
                    "median_roi": statistics.median(rois) if rois else 0.0,
                }
            )
        out.sort(key=lambda row: (-int(row["n"]), str(row["bucket"])))
        return out

    by_category: dict[str, list[SignalEvent]] = defaultdict(list)
    by_price: dict[str, list[SignalEvent]] = defaultdict(list)
    by_notional: dict[str, list[SignalEvent]] = defaultdict(list)
    by_volume: dict[str, list[SignalEvent]] = defaultdict(list)
    for event in rows:
        by_category[event.category].append(event)
        price = event.entry_price_cents
        if price < 10:
            price_bucket = "01-09c"
        elif price < 25:
            price_bucket = "10-24c"
        elif price < 50:
            price_bucket = "25-49c"
        elif price < 70:
            price_bucket = "50-69c"
        elif price < 90:
            price_bucket = "70-89c"
        else:
            price_bucket = "90-99c"
        by_price[price_bucket].append(event)
        n = event.source_notional_cents
        if n < 10_000:
            notional_bucket = "$10-$99"
        elif n < 50_000:
            notional_bucket = "$100-$499"
        elif n < 100_000:
            notional_bucket = "$500-$999"
        else:
            notional_bucket = "$1k+"
        by_notional[notional_bucket].append(event)
        volume = event.daily_volume_cents
        if volume < 1_000_000:
            volume_bucket = "<$10k"
        elif volume < 5_000_000:
            volume_bucket = "$10k-$49k"
        elif volume < 20_000_000:
            volume_bucket = "$50k-$199k"
        else:
            volume_bucket = "$200k+"
        by_volume[volume_bucket].append(event)
    return {
        "resolved_events": len(rows),
        "by_category": summarize(by_category),
        "by_entry_price": summarize(by_price),
        "by_source_notional": summarize(by_notional),
        "by_market_daily_volume": summarize(by_volume),
    }


def wallet_persistence(
    events: list[SignalEvent],
    *,
    split: datetime,
    end: datetime,
) -> dict[str, Any]:
    def stats(rows: Iterable[SignalEvent], cutoff: datetime) -> dict[str, tuple[int, float]]:
        grouped: dict[str, list[float]] = defaultdict(list)
        for event in rows:
            if (
                event.resolved_at is None
                or event.resolved_at > cutoff
                or event.resolved_outcome not in ("YES", "NO")
                or event.entry_price_cents <= 0
            ):
                continue
            won = event.outcome == event.resolved_outcome
            roi = (100 - event.entry_price_cents) / event.entry_price_cents if won else -1.0
            grouped[event.source_wallet].append(roi)
        return {wallet: (len(rois), statistics.mean(rois)) for wallet, rois in grouped.items()}

    train = stats((e for e in events if e.timestamp < split), split)
    valid = stats((e for e in events if e.timestamp >= split), end)
    shared = [w for w in train if train[w][0] >= 2 and valid.get(w, (0, 0))[0] >= 2]
    if len(shared) >= 2:
        x = [train[w][1] for w in shared]
        y = [valid[w][1] for w in shared]
        mx, my = statistics.mean(x), statistics.mean(y)
        num = sum((a - mx) * (b - my) for a, b in zip(x, y, strict=True))
        den = math.sqrt(sum((a - mx) ** 2 for a in x) * sum((b - my) ** 2 for b in y))
        correlation = num / den if den else 0.0
    else:
        correlation = None

    eligible = [(w, n, roi) for w, (n, roi) in train.items() if n >= 3]
    eligible.sort(key=lambda row: (-row[2], -row[1]))
    top_count = max(1, math.ceil(len(eligible) * 0.25)) if eligible else 0
    top_wallets = {row[0] for row in eligible[:top_count]}
    validation_rows = [
        e
        for e in events
        if e.timestamp >= split
        and e.source_wallet in top_wallets
        and e.resolved_at is not None
        and e.resolved_at <= end
        and e.resolved_outcome in ("YES", "NO")
    ]
    validation_rois = [
        ((100 - e.entry_price_cents) / e.entry_price_cents)
        if e.outcome == e.resolved_outcome
        else -1.0
        for e in validation_rows
        if e.entry_price_cents > 0
    ]
    return {
        "shared_wallets_min_2_each_window": len(shared),
        "train_validation_roi_correlation": correlation,
        "train_wallets_min_3": len(eligible),
        "top_train_quartile_wallets": len(top_wallets),
        "top_train_quartile_validation_events": len(validation_rois),
        "top_train_quartile_validation_mean_roi": (
            statistics.mean(validation_rois) if validation_rois else None
        ),
    }


def choose_split(start: datetime, end: datetime, raw: str | None) -> datetime:
    if raw:
        parsed = parse_dt(raw)
        if parsed is None:
            raise ValueError(f"invalid --split timestamp: {raw}")
        return parsed
    return start + (end - start) / 2


def print_human(report: dict[str, Any]) -> None:
    vintage = report["vintage"]
    print("=== PolySim strategy deep dive ===")
    print(f"latest trade: {vintage['latest_trade']}")
    print(f"window: {report['window']['start']} -> {report['window']['end']}")
    print(f"split:  {report['window']['split']}")
    print(f"signal rows: {vintage['external_signal_rows']}")
    print()
    print("=== Active run scorecards ===")
    print(
        f"{'id':>3} {'run':<34} {'state':>7} {'return':>9} "
        f"{'repair':>9} {'closed':>7} {'open':>5} {'win':>7}"
    )
    for row in sorted(report["runs"], key=lambda r: -r["corrected_return_pct"]):
        print(
            f"{row['run_id']:>3} {row['name'][:34]:<34} "
            f"{('paused' if row['paused'] else 'live'):>7} "
            f"{row['corrected_return_pct'] * 100:>+8.2f}% "
            f"${row['uncredited_mirror_exit_cents'] / 100:>+7.2f} "
            f"{row['closed_positions']:>7} "
            f"{row['open_positions']:>5} {row['win_rate'] * 100:>6.1f}%"
        )
    print()
    print("=== Best validation strategies (capital locked, one market max) ===")
    print(f"{'strategy':<52} {'valid':>8} {'train':>8} {'bets':>6} {'res':>5}")
    for row in report["strategy_rankings"][:15]:
        valid = row["validation"]
        train = row["train"]
        print(
            f"{row['strategy'][:52]:<52} {valid['return_pct'] * 100:>+7.2f}% "
            f"{train['return_pct'] * 100:>+7.2f}% {valid['bets_opened']:>6} "
            f"{valid['bets_resolved']:>5}"
        )
    print()
    print("=== Best $50k-liquidity validation strategies ===")
    print(f"{'strategy':<52} {'valid':>8} {'train':>8} {'bets':>6} {'res':>5}")
    for row in report["liquid_strategy_rankings"][:15]:
        valid = row["validation"]
        train = row["train"]
        print(
            f"{row['strategy'][:52]:<52} {valid['return_pct'] * 100:>+7.2f}% "
            f"{train['return_pct'] * 100:>+7.2f}% {valid['bets_opened']:>6} "
            f"{valid['bets_resolved']:>5}"
        )
    print()
    print("=== Selected candidate checks ===")
    print(f"{'strategy':<52} {'valid':>8} {'train':>8} {'bets':>6} {'res':>5}")
    for row in report["selected_strategy_checks"]:
        valid = row["validation"]
        train = row["train"]
        print(
            f"{row['strategy'][:52]:<52} {valid['return_pct'] * 100:>+7.2f}% "
            f"{train['return_pct'] * 100:>+7.2f}% {valid['bets_opened']:>6} "
            f"{valid['bets_resolved']:>5}"
        )
    print()
    persistence = report["wallet_persistence"]
    print("=== Wallet persistence ===")
    print(json.dumps(persistence, indent=2))
    print()
    print("=== Descriptive resolved-event buckets ===")
    print(json.dumps(report["resolved_buckets"], indent=2))
    print()
    print("=== Equity data and exit diagnostics ===")
    print(json.dumps(report["equity_data_vintage"], indent=2))
    for row in report["runs"]:
        if row["tag"] != "equity_v1":
            continue
        print(
            f"{row['name']}: realized=${row['realized_pnl_cents'] / 100:+,.2f} "
            f"cash=${(row['cash_cents'] or 0) / 100:,.2f} "
            f"exits={json.dumps(row['exit_breakdown'])}"
        )
        if row["pnl_by_ticker_bottom"]:
            print(f"  bottom tickers: {json.dumps(row['pnl_by_ticker_bottom'])}")
            print(f"  top tickers:    {json.dumps(row['pnl_by_ticker_top'])}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=Path("polysim.db"))
    parser.add_argument("--start", help="UTC ISO timestamp; default first tournament start")
    parser.add_argument("--end", help="UTC ISO timestamp; default latest trade")
    parser.add_argument("--split", help="UTC ISO timestamp; default midpoint")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--top", type=int, default=30)
    args = parser.parse_args()

    with connect_read_only(args.db) as conn:
        vintage = database_vintage(conn)
        start = parse_dt(args.start) if args.start else tournament_start(conn)
        end = parse_dt(args.end) if args.end else parse_dt(vintage["latest_trade"])
        if start is None or end is None or end <= start:
            raise SystemExit("invalid analysis window")
        split = choose_split(start, end, args.split)
        if not start < split < end:
            raise SystemExit("split must be inside the analysis window")

        events_by_confirmation = load_confirmation_events(conn, start=start)
        marks = MarketMarks(conn)
        rankings: list[dict[str, Any]] = []
        for spec in strategy_grid():
            events = events_by_confirmation[spec.confirmations]
            train = simulate(events, spec, start=start, end=split, marks=marks)
            validation = simulate(events, spec, start=split, end=end, marks=marks)
            full = simulate(events, spec, start=start, end=end, marks=marks)
            rankings.append(
                {
                    "strategy": spec.name,
                    "spec": asdict(spec),
                    "train": asdict(train),
                    "validation": asdict(validation),
                    "full": asdict(full),
                }
            )
        rankings.sort(
            key=lambda row: (
                -float(row["validation"]["return_pct"]),
                -int(row["validation"]["bets_resolved"]),
                -float(row["train"]["return_pct"]),
            )
        )
        tracked = selected_strategy_names()
        report = {
            "generated_at": iso(datetime.now(UTC)),
            "vintage": vintage,
            "window": {"start": iso(start), "split": iso(split), "end": iso(end)},
            "runs": run_scorecards(conn),
            "signal_event_counts": {
                str(n): len(events) for n, events in events_by_confirmation.items()
            },
            "strategy_rankings": rankings[: max(1, args.top)],
            "liquid_strategy_rankings": [
                row for row in rankings if int(row["spec"]["min_market_volume_cents"]) >= 5_000_000
            ][: max(1, args.top)],
            "selected_strategy_checks": [row for row in rankings if row["strategy"] in tracked],
            "wallet_persistence": wallet_persistence(
                events_by_confirmation[1], split=split, end=end
            ),
            "resolved_buckets": bucket_rows(events_by_confirmation[1], end),
            "equity_data_vintage": equity_data_vintage(conn),
        }

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print_human(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
