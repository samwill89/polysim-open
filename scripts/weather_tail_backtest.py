"""Backtest 'buy NO at >=95c on near-certain weather favorites'.

Uses REAL executed trade prints (Polymarket Data API) on the actual daily
temperature markets, and pays off against the actual resolution. No price
simulation — every entry price is a price real money transacted at.

Method, per market:
  - pull trade prints; convert each to a NO-side price
    (price if outcome==No, else 1-price)
  - for each threshold, collect prints where NO price in [thr, 0.999]
  - representative entry = MEDIAN qualifying NO price + SLIPPAGE_C cents
    (median, not min, so we don't cherry-pick the cheapest tick)
  - payoff per share: resolve NO -> +(1-entry); resolve YES -> -entry
  - one bet per market (equal $ stake), so high-volume markets don't dominate

Honest caveats:
  - fees: Polymarket charges 0 today, none modeled
  - liquidity/capacity NOT capped -> this is an UPPER bound on returns
  - 'buy when you see it in the band' has mild look-ahead vs a live scanner,
    which only helps the strategy -> still an upper bound
"""

from __future__ import annotations

import json
import statistics
import sys

import httpx

GAMMA = "https://gamma-api.polymarket.com"
DATA = "https://data-api.polymarket.com"
WEATHER_TAGS = (104615, 84)   # 'temperature', 'Weather'

SLIPPAGE_C = 0.3
THRESHOLDS = (0.90, 0.95, 0.97, 0.99)
STAKE = 100.0
MAX_MARKETS = int(sys.argv[1]) if len(sys.argv) > 1 else 400


def fetch_markets(client: httpx.Client, want: int) -> list[dict]:
    out: dict[str, dict] = {}
    for tag in WEATHER_TAGS:
        offset = 0
        while len(out) < want and offset < 5000:
            try:
                r = client.get(f"{GAMMA}/markets", params={
                    "closed": "true", "limit": 200, "offset": offset,
                    "tag_id": tag,
                }, timeout=40)
                r.raise_for_status()
                batch = r.json()
            except Exception:
                break
            if not batch:
                break
            for m in batch:
                op = m.get("outcomePrices")
                cid = m.get("conditionId")
                if not (op and cid):
                    continue
                op = json.loads(op) if isinstance(op, str) else op
                if {str(op[0]), str(op[1])} != {"1", "0"}:
                    continue
                m["_won_no"] = str(op[1]) == "1"
                out[cid] = m
            offset += 200
    return list(out.values())[:want]


def no_prices(client: httpx.Client, cid: str) -> list[float]:
    """All trade prints on this market, expressed as a NO-side price."""
    out: list[float] = []
    for off in (0, 100, 200):
        try:
            r = client.get(f"{DATA}/trades", params={
                "market": cid, "limit": 100, "offset": off,
            }, timeout=30)
            if r.status_code != 200:
                break
            trs = r.json()
        except Exception:
            break
        if not trs:
            break
        for t in trs:
            try:
                p = float(t.get("price"))
            except (TypeError, ValueError):
                continue
            oc = str(t.get("outcome", "")).lower()
            no_p = p if oc.startswith("n") else (1.0 - p)
            if 0.0 < no_p < 1.0:
                out.append(no_p)
        if len(trs) < 100:
            break
    return out


def main() -> None:
    client = httpx.Client(headers={"User-Agent": "polysim-bt/2"})
    print(f"fetching up to {MAX_MARKETS} resolved weather markets...",
          flush=True)
    markets = fetch_markets(client, MAX_MARKETS)
    no_res = sum(1 for m in markets if m["_won_no"])
    print(f"  got {len(markets)} clean binary weather markets "
          f"({no_res} resolved NO = "
          f"{100*no_res/max(1,len(markets)):.1f}%)\n", flush=True)

    # bets[thr] = list of (entry, won_no)
    bets: dict[float, list[tuple[float, bool]]] = {t: [] for t in THRESHOLDS}
    processed = 0
    for m in markets:
        prices = no_prices(client, m["conditionId"])
        if not prices:
            continue
        processed += 1
        if processed % 50 == 0:
            print(f"  ...processed {processed}", flush=True)
        won = m["_won_no"]
        for thr in THRESHOLDS:
            band = [p for p in prices if thr <= p <= 0.999]
            if not band:
                continue
            entry = min(0.999, statistics.median(band) + SLIPPAGE_C / 100.0)
            bets[thr].append((entry, won))

    print(f"markets with trade prints: {processed}\n")
    hdr = (f"{'thr':>5} {'bets':>5} {'winrate':>8} {'avg_entry':>9} "
           f"{'net_roi/bet':>11} {'total$':>9} {'worst$':>8} "
           f"{'1 loss =':>9}")
    print(hdr)
    print("-" * len(hdr))
    for thr in THRESHOLDS:
        b = bets[thr]
        if not b:
            print(f"{thr:>5.2f}  (no qualifying bets)")
            continue
        n = len(b)
        wins = sum(1 for _, w in b if w)
        wr = wins / n
        avg_entry = sum(e for e, _ in b) / n
        # per-bet dollar P&L on equal $STAKE notional (shares = STAKE/entry)
        pnls = [((1.0 - e) if w else (-e)) / e * STAKE for e, w in b]
        total = sum(pnls)
        mean_roi = (total / n) / STAKE
        worst = min(pnls)
        avg_win = sum(p for p in pnls if p > 0) / max(1, wins)
        avg_loss = abs(sum(p for p in pnls if p <= 0) / max(1, n - wins))
        wins_to_erase = (avg_loss / avg_win) if avg_win else float("inf")
        print(f"{thr:>5.2f} {n:>5} {wr*100:>7.1f}% {avg_entry:>9.3f} "
              f"{mean_roi*100:>10.2f}% {total:>9.0f} {worst:>8.0f} "
              f"{wins_to_erase:>7.1f}x")

    print(f"\nstake ${STAKE:.0f}/bet · slippage {SLIPPAGE_C}c · "
          f"median qualifying NO entry · 1 bet/market · liquidity NOT capped")


if __name__ == "__main__":
    main()
