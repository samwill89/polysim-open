"""Momentum/trend backtest on the AI/semis/robotics universe.

The control experiment for the proposed sentiment track: if a systematic
trend strategy can't beat simply buying-and-holding the theme (SMH / equal-
weight universe) over the last ~4 months, then bolting fragile social
sentiment on top is unlikely to save it.

Data: free daily OHLCV from Stooq (no key). Positions are equal-weighted
across the held set; the set only changes on a signal change, so buy-and-hold
trades once and trend strategies pay slippage only when membership turns over.
Fills at next-day OPEN with slippage; marks at daily closes. $10k bankroll.

Variants (each returns a target SET of tickers per day; None/empty = cash):
  SMH_BH    - hold SMH (the real benchmark / theme beta)
  EW_BH     - hold equal-weight whole universe, bought once
  TREND_EW  - hold names with close>SMA50 AND SMA20>SMA50, else cash
  TREND_HOLD- enter on trend gate, stay until close<SMA20 (sticky; hold winners)
  TOPN_MOM  - top 8 by trailing 63d return that are also >SMA50
"""

from __future__ import annotations

import statistics
from datetime import UTC, datetime

import httpx

YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/"
BANKROLL = 10_000.0
SLIPPAGE_BPS = 5.0
EVAL_TRADING_DAYS = 85
SMA_FAST, SMA_SLOW = 20, 50
MOM_LOOKBACK = 63
TOPN = 8

UNIVERSE = [
    "nvda", "amd", "avgo", "tsm", "mu", "mrvl", "arm", "qcom", "intc",
    "smci", "asml", "amat", "lrcx", "klac", "ter", "entg", "anet", "vrt",
    "tsla", "isrg", "rmbs", "wdc", "stx", "now", "pltr", "dell", "mpwr",
    "on", "nxpi", "adi", "txn", "crwd",
]
ETFS = ["smh", "soxx", "botz"]
BENCH = "smh"


def fetch(client, ticker):
    """{date_str: (open, close)} from Yahoo's chart JSON (no key)."""
    try:
        r = client.get(f"{YAHOO}{ticker.upper()}",
                       params={"range": "1y", "interval": "1d"}, timeout=40)
        if r.status_code != 200:
            return {}
        res = r.json()["chart"]["result"][0]
    except Exception:
        return {}
    ts = res.get("timestamp") or []
    q = res.get("indicators", {}).get("quote", [{}])[0]
    opens, closes = q.get("open") or [], q.get("close") or []
    out = {}
    for i, t in enumerate(ts):
        o = opens[i] if i < len(opens) else None
        c = closes[i] if i < len(closes) else None
        if o is None or c is None:
            continue
        d = datetime.fromtimestamp(t, tz=UTC).strftime("%Y-%m-%d")
        out[d] = (float(o), float(c))
    return out


def sma(vals, n):
    return sum(vals[-n:]) / n if len(vals) >= n else None


def max_dd(curve):
    peak, mdd = curve[0], 0.0
    for v in curve:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1.0)
    return mdd


def sharpe(curve):
    rets = [curve[i] / curve[i - 1] - 1.0 for i in range(1, len(curve))]
    if len(rets) < 2:
        return 0.0
    sd = statistics.pstdev(rets)
    return (statistics.mean(rets) / sd * (252 ** 0.5)) if sd > 0 else 0.0


def run(name, eval_dates, opens, closes, set_fn):
    cash, shares = BANKROLL, {}
    cur_set, curve, turnover, changes = frozenset(), [], 0.0, 0
    for i in range(len(eval_dates)):
        d = eval_dates[i]
        equity = cash + sum(s * closes[t][d] for t, s in shares.items() if d in closes[t])
        curve.append(equity)
        if i + 1 >= len(eval_dates):
            break
        want = set_fn(i)
        want = frozenset(want) if want else frozenset()
        if want == cur_set:
            continue  # hold: no trade, weights drift
        changes += 1
        fd = eval_dates[i + 1]
        cur_val = cash + sum(s * opens[t][fd] for t, s in shares.items() if fd in opens[t])
        new_shares = {}
        if want:
            tradeable = [t for t in want if fd in opens[t]]
            if tradeable:
                w = 1.0 / len(tradeable)
                for t in tradeable:
                    new_shares[t] = (cur_val * w) / opens[t][fd]
        traded = 0.0
        for t in set(new_shares) | set(shares):
            op = opens[t].get(fd)
            if op is None:
                continue
            traded += abs(new_shares.get(t, 0.0) * op - shares.get(t, 0.0) * op)
        cost = traded * SLIPPAGE_BPS / 10_000.0
        turnover += traded
        invested = sum(s * opens[t][fd] for t, s in new_shares.items())
        cash = cur_val - invested - cost
        shares, cur_set = new_shares, want
    final = curve[-1]
    return {"name": name, "ret": final / BANKROLL - 1.0, "mdd": max_dd(curve),
            "sharpe": sharpe(curve), "turnover_x": turnover / BANKROLL,
            "changes": changes, "curve": curve}


def main():
    client = httpx.Client(headers={"User-Agent": "Mozilla/5.0 polysim-bt"})
    all_t = UNIVERSE + ETFS
    print(f"fetching {len(all_t)} tickers from Stooq...", flush=True)
    data = {t: d for t in all_t if (d := fetch(client, t))}
    print(f"  got {len(data)}/{len(all_t)}", flush=True)
    if BENCH not in data:
        print("benchmark missing; abort")
        return
    cal = sorted(data[BENCH].keys())
    opens = {t: {d: data[t][d][0] for d in data[t]} for t in data}
    closes = {t: {d: data[t][d][1] for d in data[t]} for t in data}
    if len(cal) < EVAL_TRADING_DAYS + SMA_SLOW + 5:
        print(f"not enough history: {len(cal)}")
        return
    eval_dates = cal[-EVAL_TRADING_DAYS:]
    universe = [t for t in UNIVERSE if t in data
                and sum(1 for d in eval_dates if d in closes[t]) >= EVAL_TRADING_DAYS - 3]
    print(f"eval: {eval_dates[0]} -> {eval_dates[-1]} ({len(eval_dates)}d), "
          f"{len(universe)} names\n", flush=True)

    def cs_upto(t, d):
        i = cal.index(d)
        return [closes[t][x] for x in cal[:i + 1] if x in closes[t]]

    def s_smh(i):
        return {BENCH}

    def s_ew(i):
        return set(universe)

    def s_trend(i):
        d = eval_dates[i]
        out = set()
        for t in universe:
            cs = cs_upto(t, d)
            f, s = sma(cs, SMA_FAST), sma(cs, SMA_SLOW)
            if f and s and cs[-1] > s and f > s:
                out.add(t)
        return out

    held = set()

    def s_trend_hold(i):
        d = eval_dates[i]
        for t in list(held):
            cs = cs_upto(t, d)
            f = sma(cs, SMA_FAST)
            if f is None or cs[-1] < f:
                held.discard(t)
        for t in universe:
            if t in held:
                continue
            cs = cs_upto(t, d)
            f, s = sma(cs, SMA_FAST), sma(cs, SMA_SLOW)
            if f and s and cs[-1] > s and f > s:
                held.add(t)
        return set(held)

    def s_topn(i):
        d = eval_dates[i]
        ranked = []
        for t in universe:
            cs = cs_upto(t, d)
            s = sma(cs, SMA_SLOW)
            if len(cs) > MOM_LOOKBACK and s and cs[-1] > s:
                ranked.append((cs[-1] / cs[-1 - MOM_LOOKBACK] - 1.0, t))
        ranked.sort(reverse=True)
        return {t for _, t in ranked[:TOPN]}

    variants = [("SMH_BH", s_smh), ("EW_BH", s_ew), ("TREND_EW", s_trend),
                ("TREND_HOLD", s_trend_hold), ("TOPN_MOM", s_topn)]
    results = []
    for name, fn in variants:
        held.clear()
        results.append(run(name, eval_dates, opens, closes, fn))
    smh = next(r["ret"] for r in results if r["name"] == "SMH_BH")
    print(f"{'variant':<12} {'return':>8} {'vs SMH':>8} {'maxDD':>7} "
          f"{'Sharpe':>7} {'turn':>6} {'trades':>7}")
    print("-" * 62)
    for r in results:
        print(f"{r['name']:<12} {r['ret']*100:>7.2f}% {(r['ret']-smh)*100:>+7.2f}% "
              f"{r['mdd']*100:>6.1f}% {r['sharpe']:>7.2f} "
              f"{r['turnover_x']:>5.1f}x {r['changes']:>7}")
    print(f"\n$10k, fills@next-open, {SLIPPAGE_BPS}bps/side, $0 commission. "
          f"Window {eval_dates[0]}..{eval_dates[-1]}.")


if __name__ == "__main__":
    main()
