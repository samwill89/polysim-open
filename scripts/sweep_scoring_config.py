"""Grid-sweep scoring config to find the highest-yield combo.

Sweeps:
  - CategoryInsider.min_resolved_markets in {1, 2, 3, 5, 8}
  - EventInsider.fresh_size_min_cents in {1_000, 5_000, 10_000, 50_000}
  - flag_threshold in {0.5, 1.0, 1.5, 2.0, 3.0, 5.0}
  - min_contributing_detectors in {1, 2}

For each combo, on a fixed sample of recent trades:
  1. count flags
  2. for the subset of flagged trades on RESOLVED markets, count wins
     (trade.outcome side matches market.resolved_outcome)
  3. report flags / wins / win_rate / sized_edge

We score each detector once per its param value (memoized) and run the
composite/threshold check in pure Python so the sweep finishes fast.

Run from repo root:
    .venv/Scripts/python.exe scripts/sweep_scoring_config.py --limit 1500
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from itertools import product
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import aiosqlite

from polysim.config import load_config
from polysim.db import dao
from polysim.evaluator.backtest import _build_scorer
from polysim.profiler.wallet_profiler import refresh_stale_profiles
from polysim.scoring.category_insider import CategoryInsiderDetector
from polysim.scoring.composite import is_stale
from polysim.scoring.coordination import CoordinationDetector
from polysim.scoring.event_insider import EventInsiderDetector
from polysim.scoring.fresh_wallet import FreshWalletDetector
from polysim.scoring.timing import TimingDetector

# Sweep grid.
CAT_MIN_RESOLVED = [1, 2, 3, 5, 8]
EVENT_FRESH_SIZE_MIN = [1_000, 5_000, 10_000, 50_000]
FLAG_THRESHOLDS = [0.5, 1.0, 1.5, 2.0, 3.0, 5.0]
MIN_DETECTORS = [1, 2]


@dataclass
class TradeCtx:
    trade_id: str
    wallet: str
    market_id: str
    outcome: str          # YES or NO
    side: str             # BUY or SELL
    market_resolved_outcome: str | None  # YES/NO/None


async def _recent_trades(db: Path, limit: int) -> list[TradeCtx]:
    async with aiosqlite.connect(str(db)) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT t.id, t.wallet_address, t.market_id, t.outcome, t.side, "
            "m.resolved_outcome "
            "FROM trades t JOIN markets m ON m.id = t.market_id "
            "ORDER BY t.timestamp DESC LIMIT ?", (limit,)
        ) as cur:
            rows = await cur.fetchall()
    return [
        TradeCtx(
            trade_id=str(r["id"]), wallet=str(r["wallet_address"]),
            market_id=str(r["market_id"]),
            outcome=str(r["outcome"]), side=str(r["side"]),
            market_resolved_outcome=(
                str(r["resolved_outcome"]) if r["resolved_outcome"] else None
            ),
        )
        for r in rows
    ]


def _is_winning_trade(ctx: TradeCtx) -> bool | None:
    """For BUY trades on resolved markets only:
    - BUY YES → win if resolved YES
    - BUY NO  → win if resolved NO
    SELL trades excluded (closing positions, not opinion entries).
    None if market unresolved.
    """
    if ctx.market_resolved_outcome is None:
        return None
    if ctx.side != "BUY":
        return None
    return ctx.outcome.upper() == ctx.market_resolved_outcome.upper()


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=Path("polysim.db"))
    parser.add_argument("--limit", type=int, default=1500)
    args = parser.parse_args()

    cfg = load_config()
    scorer = _build_scorer(cfg)
    weights = scorer.weights

    print("refreshing profiles...", flush=True)
    n = await refresh_stale_profiles(
        args.db, staleness_seconds=0, max_wallets=10_000,
    )
    print(f"  {n} profiles refreshed")

    trades = await _recent_trades(args.db, args.limit)
    print(f"sample size: {len(trades)}", flush=True)
    n_resolved = sum(1 for t in trades if t.market_resolved_outcome)
    n_buy_resolved = sum(
        1 for t in trades
        if t.market_resolved_outcome and t.side == "BUY"
    )
    print(f"  on resolved markets: {n_resolved}  "
          f"(BUY-only ground-truth eligible: {n_buy_resolved})")

    # Pre-build the cheaper detectors once.
    fresh_det = FreshWalletDetector()
    coord_det = CoordinationDetector(
        args.db, window_hours=cfg.detectors.coordination.window_hours,
    )
    meta_baselines = await dao.meta_get_json(args.db, key="timing_baselines")
    base_rates: dict[str, float] = {}
    if isinstance(meta_baselines, dict):
        for k, v in meta_baselines.items():
            try:
                base_rates[str(k)] = float(v)
            except (TypeError, ValueError):
                continue
    timing_det = TimingDetector(
        args.db, late_window_hours=cfg.detectors.timing.late_window_hours,
        base_rates_by_category=base_rates or None,
    )
    cat_dets = {
        v: CategoryInsiderDetector(min_resolved_markets=v)
        for v in CAT_MIN_RESOLVED
    }
    event_dets = {
        v: EventInsiderDetector(
            fresh_nonce_threshold=cfg.detectors.event_insider.fresh_nonce_threshold,
            fresh_size_min_cents=v,
            niche_market_vol_max_cents=cfg.detectors.event_insider.niche_market_vol_max_cents,
            contrarian_bps=cfg.detectors.event_insider.contrarian_bps,
        )
        for v in EVENT_FRESH_SIZE_MIN
    }

    # signals[(detector_name, param_value, trade_id)] = (raw, conf, weighted)
    signals: dict[tuple[str, object, str], tuple[float, float, float]] = {}
    skipped = 0

    print("scoring per-detector signals...", flush=True)
    for i, t in enumerate(trades, start=1):
        profile = await dao.get_latest_profile(args.db, t.wallet)
        if profile is None or is_stale(profile):
            skipped += 1
            continue
        market = await dao.get_market(args.db, t.market_id)
        if market is None:
            skipped += 1
            continue
        wallet_obj = await dao.get_wallet(args.db, t.wallet)
        if wallet_obj is None:
            skipped += 1
            continue
        features = dict(profile.features)
        if wallet_obj.nonce is not None:
            features["nonce"] = int(wallet_obj.nonce)
        if wallet_obj.funding_source is not None:
            features["funding_source"] = wallet_obj.funding_source
        enriched = profile.model_copy(update={"features": features})

        all_trades_for_wallet = await dao.get_trades_by_wallet(args.db, t.wallet)
        trade_obj = next(
            (tr for tr in all_trades_for_wallet if tr.id == t.trade_id), None,
        )

        async def _score(
            det: object, name: str, key: object,
            *,
            _enriched: object = None, _market: object = None,
            _trade_obj: object = None, _trade_id: str,
        ) -> None:
            try:
                sig = await det.score(_enriched, _market, _trade_obj)  # type: ignore[attr-defined]
            except Exception:
                return
            if sig is None:
                return
            w = float(weights.get(name, 0.0))
            signals[(name, key, _trade_id)] = (
                float(sig.raw_score), float(sig.confidence),
                w * float(sig.raw_score) * float(sig.confidence),
            )

        kwargs: dict[str, object] = {
            "_enriched": enriched, "_market": market, "_trade_obj": trade_obj,
            "_trade_id": t.trade_id,
        }
        await _score(fresh_det, "FreshWalletDetector", "default", **kwargs)
        await _score(coord_det, "CoordinationDetector", "default", **kwargs)
        await _score(timing_det, "TimingDetector", "default", **kwargs)
        for v, det in cat_dets.items():
            await _score(det, "CategoryInsiderDetector", v, **kwargs)
        for v, det in event_dets.items():
            await _score(det, "EventInsiderDetector", v, **kwargs)

        if i % 250 == 0:
            print(f"  {i}/{len(trades)}", flush=True)

    print(f"  skipped (no profile/market/wallet): {skipped}")
    print(f"signals computed: {len(signals)}")
    print()

    # ── Sweep ────────────────────────────────────────────
    print("sweeping configs...")
    results: list[dict[str, object]] = []
    for cat_v, evt_v, thr, min_det in product(
        CAT_MIN_RESOLVED, EVENT_FRESH_SIZE_MIN, FLAG_THRESHOLDS, MIN_DETECTORS,
    ):
        flags = 0
        wins = 0
        eligible = 0
        for t in trades:
            comp = 0.0
            contributing: list[str] = []
            for name, key in (
                ("CategoryInsiderDetector", cat_v),
                ("EventInsiderDetector", evt_v),
                ("FreshWalletDetector", "default"),
                ("CoordinationDetector", "default"),
                ("TimingDetector", "default"),
            ):
                sig = signals.get((name, key, t.trade_id))
                if sig is None:
                    continue
                raw, _conf, weighted = sig
                w = float(weights.get(name, 0.0))
                comp += weighted
                if raw > 0.0 and w > 0.0:
                    contributing.append(name)
            if comp >= thr and len(contributing) >= min_det:
                flags += 1
                truth = _is_winning_trade(t)
                if truth is not None:
                    eligible += 1
                    if truth:
                        wins += 1
        win_rate = (wins / eligible) if eligible else 0.0
        results.append({
            "cat_min_resolved": cat_v,
            "event_min_size_cents": evt_v,
            "flag_threshold": thr,
            "min_detectors": min_det,
            "flags": flags,
            "eligible": eligible,
            "wins": wins,
            "win_rate": win_rate,
            "expected_signal": flags * win_rate,
        })

    # Sort: most flags with positive resolved win-rate, then by win-rate.
    print()
    print("Top 25 configs by flags-x-win_rate (expected_signal):")
    print(f"  {'cat':>3}  {'evt$':>6}  {'thr':>4}  {'minD':>4}  "
          f"{'flags':>5}  {'elig':>5}  {'wins':>4}  {'win%':>5}  {'exp':>6}")
    print("  " + "-" * 65)
    results.sort(key=lambda r: (-(r["expected_signal"] or 0), -(r["flags"] or 0)))
    for r in results[:25]:
        print(
            f"  {r['cat_min_resolved']:>3}  "
            f"{r['event_min_size_cents']:>6}  "
            f"{r['flag_threshold']:>4.1f}  "
            f"{r['min_detectors']:>4}  "
            f"{r['flags']:>5}  "
            f"{r['eligible']:>5}  "
            f"{r['wins']:>4}  "
            f"{r['win_rate']*100:>4.1f}%  "
            f"{r['expected_signal']:>6.2f}"
        )
    print()
    print("Most flags (any quality):")
    by_flags = sorted(results, key=lambda r: -(r["flags"] or 0))[:5]
    for r in by_flags:
        print(
            f"  cat={r['cat_min_resolved']} "
            f"evt={r['event_min_size_cents']} "
            f"thr={r['flag_threshold']:.1f} "
            f"minD={r['min_detectors']} "
            f"→ flags={r['flags']} elig={r['eligible']} "
            f"wins={r['wins']} ({r['win_rate']*100:.1f}%)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
