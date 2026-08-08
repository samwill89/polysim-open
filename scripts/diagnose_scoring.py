"""Diagnose why the live replay produced 0 flags over 11k trades.

Runs the same detector + composite stack as `polysim replay`, but instead
of writing flags it records every (composite_score, contributing_detectors)
and emits:

  * histogram of composite scores (so we see the top of the distribution)
  * per-detector hit rate (how often each detector returned a non-zero
    raw_score) — tells us which detectors are silent on live data
  * top-20 trades by composite_score with full per-detector breakdown

Run from repo root:
    .venv/Scripts/python.exe scripts/diagnose_scoring.py --limit 2000

Defaults to the most-recent 2000 trades for speed.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import aiosqlite

from polysim.config import load_config
from polysim.db import dao
from polysim.evaluator.backtest import _build_detectors, _build_scorer
from polysim.profiler.wallet_profiler import refresh_stale_profiles
from polysim.scoring.composite import is_stale


async def _recent_trades(db: Path, limit: int) -> list[dict[str, str]]:
    async with aiosqlite.connect(str(db)) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT id, wallet_address, market_id, timestamp FROM trades "
            "ORDER BY timestamp DESC LIMIT ?", (limit,)
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=Path("polysim.db"))
    parser.add_argument("--limit", type=int, default=2000)
    args = parser.parse_args()

    cfg = load_config()
    detectors = await _build_detectors(args.db, cfg)
    scorer = _build_scorer(cfg)

    print("refreshing stale profiles (this may take a minute)...", flush=True)
    n = await refresh_stale_profiles(
        args.db, staleness_seconds=0, max_wallets=10_000,
    )
    print(f"  {n} profiles refreshed")

    rows = await _recent_trades(args.db, args.limit)
    print(f"scoring {len(rows)} trades...", flush=True)

    # Tallies.
    composite_hist: Counter[int] = Counter()    # bucket score floored to int
    detector_hits: Counter[str] = Counter()
    detector_total: Counter[str] = Counter()
    detector_score_sum: dict[str, float] = defaultdict(float)
    skipped: Counter[str] = Counter()
    top: list[tuple[float, str, str, dict[str, object]]] = []

    for i, t in enumerate(rows, start=1):
        wallet = str(t["wallet_address"])
        market_id = str(t["market_id"])
        trade_id = str(t["id"])

        profile = await dao.get_latest_profile(args.db, wallet)
        if profile is None or is_stale(profile):
            skipped["stale_or_missing_profile"] += 1
            continue
        market = await dao.get_market(args.db, market_id)
        if market is None:
            skipped["missing_market"] += 1
            continue
        wallet_obj = await dao.get_wallet(args.db, wallet)
        if wallet_obj is None:
            skipped["missing_wallet"] += 1
            continue

        # Mirror score_and_persist's enrichment.
        features = dict(profile.features)
        if wallet_obj.nonce is not None:
            features["nonce"] = int(wallet_obj.nonce)
        if wallet_obj.funding_source is not None:
            features["funding_source"] = wallet_obj.funding_source
        enriched = profile.model_copy(update={"features": features})

        # We only need the trade row for detectors that read it.
        trades_for_wallet = await dao.get_trades_by_wallet(args.db, wallet)
        trade_obj = next((tr for tr in trades_for_wallet if tr.id == trade_id), None)

        signals = []
        per_det: dict[str, dict[str, float]] = {}
        for d in detectors:
            name = type(d).__name__
            detector_total[name] += 1
            try:
                sig = await d.score(enriched, market, trade_obj)
            except Exception as exc:
                skipped[f"detector_raise:{name}:{type(exc).__name__}"] += 1
                signals.append(None)
                continue
            signals.append(sig)
            if sig is not None and sig.raw_score > 0.0:
                detector_hits[name] += 1
                detector_score_sum[name] += float(sig.raw_score)
                per_det[name] = {
                    "raw": float(sig.raw_score),
                    "conf": float(sig.confidence),
                }

        composite = scorer.compose(
            wallet_address=wallet, market_id=market_id, signals=signals,
        )
        bucket = int(composite.score)
        composite_hist[bucket] += 1
        if composite.score > 0:
            top.append((
                composite.score, wallet, market_id,
                {
                    "score": composite.score,
                    "contributing": composite.contributing_detectors,
                    "per_det": per_det,
                    "would_flag": scorer.should_flag(composite),
                },
            ))

        if i % 250 == 0:
            print(f"  {i}/{len(rows)} scored", flush=True)

    print()
    print(f"=== Results over {len(rows)} trades ===")
    print()
    print("Composite score histogram (floor):")
    for bucket in sorted(composite_hist):
        bar = "#" * min(60, composite_hist[bucket])
        print(f"  {bucket:>2}.0-{bucket}.9  {composite_hist[bucket]:>5}  {bar}")
    print()
    print("Per-detector hit rate (raw_score > 0 / total):")
    for name in sorted(detector_total):
        hits = detector_hits[name]
        total = detector_total[name]
        avg = detector_score_sum[name] / hits if hits else 0.0
        pct = (100 * hits / total) if total else 0.0
        print(f"  {name:<30}  {hits:>5}/{total:<5}  ({pct:5.1f}%)  avg_raw={avg:.2f}")
    print()
    print("Skipped reasons:")
    for k in sorted(skipped):
        print(f"  {k:<40}  {skipped[k]}")
    print()
    top.sort(key=lambda r: -r[0])
    print(f"Top 20 composite scores (would_flag with current gate "
          f"thr={scorer.flag_threshold}, min_det={scorer.min_contributing_detectors}):")
    for score, wallet, market, info in top[:20]:
        flag = "FLAG" if info["would_flag"] else "    "
        contrib = ",".join(info["contributing"]) or "-"
        print(f"  {flag} {score:5.2f}  {wallet[:14]}.. {market[:12]}.. {contrib}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
