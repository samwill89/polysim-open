"""Walk-forward test of the actual PolySim equity momentum exit ladder.

Unlike ``equity_momentum_backtest.py``, this model includes the live strategy's
hard stop, ATR trail, moving-average break, calendar time stop, position and
theme caps. Signals are formed at a daily close and filled at the next open
with slippage, which is slightly more conservative than the live close fill.

The candidates are pre-declared and evaluated on separate train/validation
halves. The validation half is not used to tune a parameter grid.
"""

from __future__ import annotations

import argparse
import sqlite3
import statistics
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import httpx

from polysim.equity.universe import PRIMARY_BENCHMARK, seed_symbols, theme_of

YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/"
STARTING_CASH = 10_000.0
SLIPPAGE_BPS = 5.0
MOMENTUM_DAYS = 63
WARMUP_DAYS = 70


@dataclass(frozen=True)
class Bar:
    day: str
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class Indicator:
    close: float
    sma20: float | None
    sma50: float | None
    momentum: float | None
    ret5: float | None
    atr14: float | None

    @property
    def uptrend(self) -> bool:
        return (
            self.sma20 is not None
            and self.sma50 is not None
            and self.close > self.sma50
            and self.sma20 > self.sma50
        )


@dataclass(frozen=True)
class Candidate:
    name: str
    max_positions: int = 8
    hard_stop_pct: float = 0.08
    atr_mult: float = 3.0
    trail_sma: int = 20
    time_stop_days: int = 10
    max_5d_runup: float | None = None
    per_position_cap_pct: float = 0.15
    sector_cap_pct: float = 0.40
    require_benchmark_uptrend: bool = False
    exit_on_benchmark_break: bool = False


@dataclass
class Position:
    ticker: str
    shares: float
    entry: float
    high_water: float
    opened: date


@dataclass(frozen=True)
class Result:
    name: str
    start: str
    end: str
    return_pct: float
    max_drawdown_pct: float
    sharpe: float
    trades: int
    turnover_x: float
    ending_value: float


CANDIDATES = (
    Candidate(name="live_momentum_10d"),
    Candidate(name="momentum_30d", time_stop_days=30),
    Candidate(name="momentum_63d", time_stop_days=63),
    Candidate(
        name="momentum_slow_63d",
        hard_stop_pct=0.12,
        atr_mult=4.0,
        trail_sma=50,
        time_stop_days=63,
    ),
    Candidate(
        name="momentum_slow_126d",
        hard_stop_pct=0.12,
        atr_mult=4.0,
        trail_sma=50,
        time_stop_days=126,
    ),
    Candidate(
        name="momentum_top5_slow",
        max_positions=5,
        hard_stop_pct=0.12,
        atr_mult=4.0,
        trail_sma=50,
        time_stop_days=63,
        sector_cap_pct=0.50,
    ),
    Candidate(
        name="momentum_runup_guard",
        time_stop_days=30,
        max_5d_runup=0.10,
    ),
    Candidate(
        name="momentum_regime_slow",
        hard_stop_pct=0.12,
        atr_mult=4.0,
        trail_sma=50,
        time_stop_days=63,
        require_benchmark_uptrend=True,
        exit_on_benchmark_break=True,
    ),
    Candidate(
        name="momentum_regime_top5",
        max_positions=5,
        hard_stop_pct=0.12,
        atr_mult=4.0,
        trail_sma=50,
        time_stop_days=63,
        sector_cap_pct=0.50,
        require_benchmark_uptrend=True,
        exit_on_benchmark_break=True,
    ),
)


def fetch(client: httpx.Client, ticker: str) -> list[Bar]:
    try:
        response = client.get(
            f"{YAHOO}{ticker}",
            params={"range": "2y", "interval": "1d"},
            timeout=40,
        )
        response.raise_for_status()
        result = response.json()["chart"]["result"][0]
    except Exception:
        return []
    timestamps = result.get("timestamp") or []
    quote = result.get("indicators", {}).get("quote", [{}])[0]
    opens = quote.get("open") or []
    highs = quote.get("high") or []
    lows = quote.get("low") or []
    closes = quote.get("close") or []
    rows: list[Bar] = []
    for index, stamp in enumerate(timestamps):
        values = [
            series[index] if index < len(series) else None
            for series in (opens, highs, lows, closes)
        ]
        if any(value is None for value in values):
            continue
        day = datetime.fromtimestamp(stamp, tz=UTC).strftime("%Y-%m-%d")
        rows.append(Bar(day, *(float(value) for value in values)))
    return rows


def load_cached_quotes(db_path: Path) -> dict[str, list[Bar]]:
    """Load the exact OHLC rows used by the deployed equity executor."""
    conn = sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT ticker, date, open_cents, high_cents, low_cents, close_cents "
            "FROM equity_quotes ORDER BY ticker, date"
        ).fetchall()
    finally:
        conn.close()
    out: dict[str, list[Bar]] = {}
    for row in rows:
        out.setdefault(str(row["ticker"]), []).append(
            Bar(
                day=str(row["date"]),
                open=int(row["open_cents"]) / 100.0,
                high=int(row["high_cents"]) / 100.0,
                low=int(row["low_cents"]) / 100.0,
                close=int(row["close_cents"]) / 100.0,
            )
        )
    return out


def mean(values: list[float], size: int) -> float | None:
    return statistics.mean(values[-size:]) if len(values) >= size else None


def build_indicators(rows: list[Bar]) -> dict[str, Indicator]:
    out: dict[str, Indicator] = {}
    closes: list[float] = []
    true_ranges: list[float] = []
    previous_close: float | None = None
    for bar in rows:
        closes.append(bar.close)
        true_range = bar.high - bar.low
        if previous_close is not None:
            true_range = max(
                true_range,
                abs(bar.high - previous_close),
                abs(bar.low - previous_close),
            )
        true_ranges.append(true_range)
        momentum = (
            bar.close / closes[-MOMENTUM_DAYS - 1] - 1.0 if len(closes) > MOMENTUM_DAYS else None
        )
        ret5 = bar.close / closes[-6] - 1.0 if len(closes) > 5 else None
        out[bar.day] = Indicator(
            close=bar.close,
            sma20=mean(closes, 20),
            sma50=mean(closes, 50),
            momentum=momentum,
            ret5=ret5,
            atr14=mean(true_ranges, 14),
        )
        previous_close = bar.close
    return out


def max_drawdown(curve: list[float]) -> float:
    if not curve:
        return 0.0
    peak = curve[0]
    worst = 0.0
    for value in curve:
        peak = max(peak, value)
        worst = min(worst, value / peak - 1.0)
    return worst


def sharpe(curve: list[float]) -> float:
    returns = [curve[i] / curve[i - 1] - 1.0 for i in range(1, len(curve))]
    if len(returns) < 2:
        return 0.0
    deviation = statistics.pstdev(returns)
    if deviation == 0.0:
        return 0.0
    return statistics.mean(returns) / deviation * (252**0.5)


def bar_maps(data: dict[str, list[Bar]]) -> dict[str, dict[str, Bar]]:
    return {ticker: {bar.day: bar for bar in rows} for ticker, rows in data.items()}


def portfolio_value(
    cash: float,
    positions: dict[str, Position],
    day: str,
    bars: dict[str, dict[str, Bar]],
    *,
    use_open: bool = False,
) -> float:
    total = cash
    for ticker, position in positions.items():
        bar = bars.get(ticker, {}).get(day)
        if bar is None:
            total += position.shares * position.entry
        else:
            total += position.shares * (bar.open if use_open else bar.close)
    return total


def should_exit(
    candidate: Candidate,
    position: Position,
    indicator: Indicator,
    current_day: date,
) -> bool:
    close = indicator.close
    if close <= position.entry * (1.0 - candidate.hard_stop_pct):
        return True
    if (
        indicator.atr14 is not None
        and close <= position.high_water - candidate.atr_mult * indicator.atr14
    ):
        return True
    trail = indicator.sma20 if candidate.trail_sma == 20 else indicator.sma50
    if trail is not None and close < trail:
        return True
    return (current_day - position.opened).days >= candidate.time_stop_days


def simulate(
    candidate: Candidate,
    *,
    days: list[str],
    start_index: int,
    end_index: int,
    bars: dict[str, dict[str, Bar]],
    indicators: dict[str, dict[str, Indicator]],
    symbols: list[str],
) -> Result:
    cash = STARTING_CASH
    positions: dict[str, Position] = {}
    curve: list[float] = []
    trades = 0
    turnover = 0.0
    slip = SLIPPAGE_BPS / 10_000.0

    for index in range(start_index, end_index):
        day = days[index]
        next_day = days[index + 1]
        current_date = date.fromisoformat(day)
        equity = portfolio_value(cash, positions, day, bars)
        curve.append(equity)

        exit_tickers: list[str] = []
        benchmark_indicator = indicators[PRIMARY_BENCHMARK].get(day)
        risk_off = candidate.require_benchmark_uptrend and (
            benchmark_indicator is None or not benchmark_indicator.uptrend
        )
        for ticker, position in positions.items():
            indicator = indicators.get(ticker, {}).get(day)
            if indicator is None:
                continue
            position.high_water = max(position.high_water, indicator.close)
            if (risk_off and candidate.exit_on_benchmark_break) or should_exit(
                candidate, position, indicator, current_date
            ):
                exit_tickers.append(ticker)
        for ticker in exit_tickers:
            bar = bars.get(ticker, {}).get(next_day)
            if bar is None:
                continue
            position = positions.pop(ticker)
            proceeds = position.shares * bar.open * (1.0 - slip)
            turnover += position.shares * bar.open
            cash += proceeds
            trades += 1

        next_equity = portfolio_value(cash, positions, next_day, bars, use_open=True)
        slots = candidate.max_positions - len(positions)
        if slots <= 0 or next_equity <= 0 or risk_off:
            continue

        candidates: list[tuple[float, str, Indicator]] = []
        for ticker in symbols:
            if ticker in positions:
                continue
            indicator = indicators.get(ticker, {}).get(day)
            next_bar = bars.get(ticker, {}).get(next_day)
            if (
                indicator is None
                or next_bar is None
                or not indicator.uptrend
                or indicator.momentum is None
            ):
                continue
            if (
                candidate.max_5d_runup is not None
                and indicator.ret5 is not None
                and indicator.ret5 > candidate.max_5d_runup
            ):
                continue
            candidates.append((indicator.momentum, ticker, indicator))
        candidates.sort(reverse=True)

        sector_values: dict[str, float] = {}
        for ticker, position in positions.items():
            bar = bars.get(ticker, {}).get(next_day)
            value = position.shares * (bar.open if bar else position.entry)
            sector = theme_of(ticker)
            sector_values[sector] = sector_values.get(sector, 0.0) + value

        base = next_equity / candidate.max_positions
        position_cap = next_equity * candidate.per_position_cap_pct
        sector_cap = next_equity * candidate.sector_cap_pct
        for _, ticker, _ in candidates:
            if slots <= 0:
                break
            next_bar = bars[ticker][next_day]
            sector = theme_of(ticker)
            current_sector = sector_values.get(sector, 0.0)
            target = min(base, position_cap, sector_cap - current_sector, cash)
            if target < next_equity * 0.02:
                continue
            fill = next_bar.open * (1.0 + slip)
            shares = target / fill
            cost = shares * fill
            if shares <= 0 or cost > cash + 1e-8:
                continue
            positions[ticker] = Position(
                ticker=ticker,
                shares=shares,
                entry=fill,
                high_water=fill,
                opened=date.fromisoformat(next_day),
            )
            cash -= cost
            turnover += cost
            sector_values[sector] = current_sector + shares * next_bar.open
            trades += 1
            slots -= 1

    final_day = days[end_index]
    ending = portfolio_value(cash, positions, final_day, bars)
    curve.append(ending)
    return Result(
        name=candidate.name,
        start=days[start_index],
        end=final_day,
        return_pct=ending / STARTING_CASH - 1.0,
        max_drawdown_pct=max_drawdown(curve),
        sharpe=sharpe(curve),
        trades=trades,
        turnover_x=turnover / STARTING_CASH,
        ending_value=ending,
    )


def buy_and_hold(
    ticker: str,
    *,
    name: str,
    days: list[str],
    start_index: int,
    end_index: int,
    bars: dict[str, dict[str, Bar]],
) -> Result:
    entry_day = days[start_index + 1]
    entry = bars[ticker][entry_day].open * (1.0 + SLIPPAGE_BPS / 10_000.0)
    shares = STARTING_CASH / entry
    curve = [
        shares * bars[ticker][day].close
        for day in days[start_index + 1 : end_index + 1]
        if day in bars[ticker]
    ]
    ending = curve[-1] if curve else STARTING_CASH
    return Result(
        name=name,
        start=entry_day,
        end=days[end_index],
        return_pct=ending / STARTING_CASH - 1.0,
        max_drawdown_pct=max_drawdown(curve),
        sharpe=sharpe(curve),
        trades=1,
        turnover_x=1.0,
        ending_value=ending,
    )


def print_window(label: str, results: list[Result], benchmark_return: float) -> None:
    print(f"\n=== {label} ===")
    print(
        f"{'strategy':<24} {'return':>9} {'vs SMH':>9} {'maxDD':>8} "
        f"{'Sharpe':>7} {'turn':>7} {'trades':>7}"
    )
    print("-" * 78)
    for result in sorted(results, key=lambda row: -row.return_pct):
        print(
            f"{result.name:<24} {result.return_pct * 100:>+8.2f}% "
            f"{(result.return_pct - benchmark_return) * 100:>+8.2f}% "
            f"{result.max_drawdown_pct * 100:>7.2f}% {result.sharpe:>7.2f} "
            f"{result.turnover_x:>6.1f}x {result.trades:>7}"
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--eval-days", type=int, default=252)
    parser.add_argument("--db", type=Path, help="Use cached equity_quotes instead of Yahoo")
    parser.add_argument("--start", help="First signal date (YYYY-MM-DD)")
    args = parser.parse_args()

    symbols = seed_symbols()
    requested = [*symbols, PRIMARY_BENCHMARK]
    if args.db:
        print(f"loading cached quotes from {args.db}...")
        data = load_cached_quotes(args.db)
        print(f"  got {len(data)} symbols")
    else:
        print(f"fetching {len(requested)} symbols from Yahoo...")
        data = {}
        with httpx.Client(headers={"User-Agent": "Mozilla/5.0 PolySim research"}) as client:
            for ticker in requested:
                rows = fetch(client, ticker)
                if rows:
                    data[ticker] = rows
        print(f"  got {len(data)}/{len(requested)}")
    if PRIMARY_BENCHMARK not in data:
        raise SystemExit("benchmark data unavailable")

    bars = bar_maps(data)
    indicators = {ticker: build_indicators(rows) for ticker, rows in data.items()}
    days = [bar.day for bar in data[PRIMARY_BENCHMARK]]
    if args.start:
        try:
            full_start = next(i for i, day in enumerate(days) if day >= args.start)
        except StopIteration as exc:
            raise SystemExit("--start is after available quote history") from exc
        eval_days = len(days) - full_start - 1
    else:
        eval_days = min(args.eval_days, len(days) - WARMUP_DAYS - 2)
        full_start = len(days) - eval_days - 1
    minimum_days = 20 if args.start else 80
    if eval_days < minimum_days:
        raise SystemExit("insufficient history")
    split = full_start + eval_days // 2
    full_end = len(days) - 1
    tradeable = [ticker for ticker in symbols if ticker in data]

    windows = {
        "TRAIN": (full_start, split),
        "VALIDATION": (split, full_end),
        "FULL": (full_start, full_end),
    }
    all_results: dict[str, list[Result]] = {}
    for label, (start_index, end_index) in windows.items():
        benchmark = buy_and_hold(
            PRIMARY_BENCHMARK,
            name="SMH_BH",
            days=days,
            start_index=start_index,
            end_index=end_index,
            bars=bars,
        )
        results = [benchmark]
        results.extend(
            simulate(
                candidate,
                days=days,
                start_index=start_index,
                end_index=end_index,
                bars=bars,
                indicators=indicators,
                symbols=tradeable,
            )
            for candidate in CANDIDATES
        )
        all_results[label] = results
        print_window(label, results, benchmark.return_pct)

    train = {row.name: row for row in all_results["TRAIN"]}
    validation = {row.name: row for row in all_results["VALIDATION"]}
    eligible = [
        name
        for name in train
        if name != "SMH_BH" and train[name].return_pct > 0 and validation[name].return_pct > 0
    ]
    eligible.sort(
        key=lambda name: (
            -validation[name].return_pct,
            -train[name].return_pct,
        )
    )
    print("\n=== Selection gate ===")
    if eligible:
        print(
            "positive in both halves: "
            + ", ".join(eligible)
            + f"\nleading validation candidate: {eligible[0]}"
        )
    else:
        print("no candidate was positive in both halves; keep the lane parked")
    print(
        f"window: {days[full_start]} -> {days[full_end]} | "
        f"split: {days[split]} | symbols: {len(tradeable)} | "
        f"fills: next-open + {SLIPPAGE_BPS:.1f} bps/side"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
