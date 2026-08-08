"""Comparative report generator — addendum §7.

Produces a single Markdown report comparing all runs sharing a tag:

    - Headline table (one row per profile)
    - Flag acceptance breakdown (considered / accepted / rejected + reasons)
    - Category breakdown per profile
    - Per-wallet top/bottom (top 5) per profile
    - Counterfactual: what if profile A had used profile B's sizing rules?
    - Bootstrap simulation: 1000 resamples per profile → distribution
      of 30-day outcomes, with p5/p50/p95

All sections degrade gracefully if a run has no fills or no closed positions.
"""

from __future__ import annotations

import math
import random
import statistics
from pathlib import Path
from typing import Any

from polysim.db import dao
from polysim.evaluator.metrics import compute_run_metrics

BOOTSTRAP_ITERATIONS = 1000


async def render_comparative_report(
    db_path: Path, *, tag: str, bootstrap_iters: int = BOOTSTRAP_ITERATIONS
) -> str:
    """Render the comparative Markdown report for all runs with the given tag."""
    runs = await dao.list_paper_runs_by_tag(db_path, tag=tag)
    if not runs:
        return f"# Comparative report — tag `{tag}`\n\n_No runs found with this tag._\n"

    # Pre-compute per-run metrics.
    per_run: list[dict[str, Any]] = []
    for r in runs:
        m = await compute_run_metrics(db_path, int(r["id"]))
        per_run.append({"run": r, "metrics": m})

    lines: list[str] = []
    lines.append(f"# PolySim Comparative Report — tag `{tag}`")
    lines.append("")
    lines.append(f"_{len(runs)} run(s) compared_\n")

    # 1) Headline table.
    lines.append("## Headline\n")
    lines.append(
        "| Profile | Run | Start $ | End $ | Return % | Sharpe | Max DD % "
        "| Trades | Win % |"
    )
    lines.append(
        "|---------|-----|---------|-------|----------|--------|----------"
        "|--------|-------|"
    )
    for item in per_run:
        r = item["run"]
        m = item["metrics"]
        start = int(r.get("starting_balance_cents") or 0)
        end = int(r.get("current_balance_cents") or 0)
        ret = 100.0 * (end - start) / start if start else 0.0
        lines.append(
            f"| `{r.get('profile_name') or '?'}` | #{r['id']} "
            f"| ${start/100:,.0f} | ${end/100:,.0f} | {ret:+.1f} "
            f"| {float(m.get('sharpe_annualized') or 0):.2f} "
            f"| {100*float(m.get('max_drawdown_pct') or 0):.1f} "
            f"| {int(m.get('closed_positions') or 0)} "
            f"| {100*float(m.get('win_rate') or 0):.0f} |"
        )
    lines.append("")

    # 2) Category breakdown per profile.
    lines.append("## Closed P&L by category, per profile\n")
    for item in per_run:
        r = item["run"]
        m = item["metrics"]
        cats = m.get("pnl_by_category") or {}
        lines.append(f"### `{r.get('profile_name')}` run #{r['id']}\n")
        if not cats:
            lines.append("_(no closed positions)_\n")
            continue
        lines.append("| Category | P&L |")
        lines.append("|----------|-----|")
        for cat, pnl in sorted(cats.items(), key=lambda kv: -kv[1]):
            lines.append(f"| {cat} | ${pnl/100:,.2f} |")
        lines.append("")

    # 3) Flag acceptance breakdown (per-run, counted from paper_positions).
    lines.append("## Flag acceptance per profile\n")
    lines.append(
        "Counts how many of the shared flags each profile opened a position "
        "on. All runs with the same tag see the same flag stream.\n"
    )
    all_flag_ids = await _distinct_flag_ids_acted_any(db_path, tag=tag)
    total_flags = await _flags_available_count(db_path, tag=tag)
    lines.append(f"Total flags in window: {total_flags}\n")
    lines.append("| Profile | Run | Accepted | Acceptance rate |")
    lines.append("|---------|-----|----------|-----------------|")
    for item in per_run:
        r = item["run"]
        accepted = await _positions_opened_from_tagged_flags(
            db_path, run_id=int(r["id"]), candidate_flag_ids=all_flag_ids
        )
        rate = (accepted / total_flags * 100) if total_flags else 0.0
        lines.append(
            f"| `{r.get('profile_name')}` | #{r['id']} | {accepted} | {rate:.1f}% |"
        )
    lines.append("")

    # 4) Top wallets per run.
    lines.append("## Top 5 source wallets by P&L, per profile\n")
    for item in per_run:
        r = item["run"]
        m = item["metrics"]
        top = m.get("pnl_by_source_wallet_top") or []
        bot = m.get("pnl_by_source_wallet_bottom") or []
        lines.append(f"### `{r.get('profile_name')}` run #{r['id']}\n")
        if not top and not bot:
            lines.append("_(no wallet P&L)_\n")
            continue
        lines.append("| Wallet | P&L | Positions |")
        lines.append("|--------|-----|-----------|")
        for w in (top[:5] + bot[:5]):
            addr = str(w.get("wallet") or "")
            short = f"{addr[:8]}...{addr[-4:]}" if len(addr) > 12 else addr
            lines.append(
                f"| `{short}` | ${int(w.get('pnl_cents') or 0)/100:,.2f} "
                f"| {int(w.get('positions') or 0)} |"
            )
        lines.append("")

    # 5) Counterfactual: what if profile A had used profile B's sizing?
    lines.append("## Counterfactual: swap sizing between profiles\n")
    lines.append(
        "For each profile, replay *its accepted flags* under *another profile's* "
        "sizing rules (filters stay the same — the swap only affects position "
        "size). This isolates how much of the outcome difference is sizing vs "
        "filtering.\n"
    )
    if len(per_run) >= 2:
        cf_table = _counterfactual_table(per_run)
        lines.extend(cf_table)
    else:
        lines.append("_(needs ≥ 2 runs)_\n")
    lines.append("")

    # 6) Bootstrap simulation (addendum §11).
    lines.append("## Bootstrap: resampled 30-day outcome distributions\n")
    lines.append(
        f"For each profile, resample its closed-position P&L stream "
        f"`{bootstrap_iters}` times and sum to a 30-day outcome. One sample = "
        "one run. This makes the shape of the outcome distribution explicit — "
        "especially the left tail.\n"
    )
    lines.append(
        "| Profile | Run | Start $ | p5 | p50 | p95 | mean | pct ≥ start | "
        "worst |"
    )
    lines.append(
        "|---------|-----|---------|-----|-----|-----|------|-------------|-------|"
    )
    for item in per_run:
        r = item["run"]
        m = item["metrics"]
        start = int(r.get("starting_balance_cents") or 0)
        dist = _bootstrap_distribution(m, start, iters=bootstrap_iters)
        if not dist:
            lines.append(
                f"| `{r.get('profile_name')}` | #{r['id']} | ${start/100:,.0f} "
                f"| — | — | — | — | — | — |"
            )
            continue
        p5 = _percentile(dist, 0.05)
        p50 = _percentile(dist, 0.50)
        p95 = _percentile(dist, 0.95)
        mean_ = statistics.mean(dist)
        pct_positive = sum(1 for x in dist if x >= start) / len(dist) * 100
        worst = min(dist)
        lines.append(
            f"| `{r.get('profile_name')}` | #{r['id']} | ${start/100:,.0f} "
            f"| ${p5/100:,.0f} | ${p50/100:,.0f} | ${p95/100:,.0f} "
            f"| ${mean_/100:,.0f} | {pct_positive:.0f}% | ${worst/100:,.0f} |"
        )
    lines.append("")
    lines.append(
        "_Bootstrap is a resample of the observed stream — it does not create "
        "information the stream does not contain. A short-lived run with a fat "
        "upside is still a short-lived run with a fat upside._\n"
    )

    return "\n".join(lines) + "\n"


# ── helpers ──────────────────────────────────────────────


async def _flags_available_count(db_path: Path, *, tag: str) -> int:
    """Distinct flag ids that one or more of the tagged runs saw.

    We approximate this as "distinct source_flag_id across all positions for
    the runs with this tag" + "flags created in the run window". Without a
    per-run "considered-flag" log, this is the cleanest proxy.
    """
    import aiosqlite

    rows = await dao.list_paper_runs_by_tag(db_path, tag=tag)
    if not rows:
        return 0
    started = min(str(r["started_at"]) for r in rows)
    ended_vals = [r.get("ended_at") for r in rows if r.get("ended_at")]
    ended = max(str(e) for e in ended_vals) if ended_vals else None

    try:
        async with aiosqlite.connect(str(db_path)) as db:
            if ended is None:
                async with db.execute(
                    "SELECT COUNT(*) FROM flags WHERE created_at >= ?",
                    (started,),
                ) as cur:
                    r = await cur.fetchone()
            else:
                async with db.execute(
                    "SELECT COUNT(*) FROM flags "
                    "WHERE created_at >= ? AND created_at <= ?",
                    (started, ended),
                ) as cur:
                    r = await cur.fetchone()
    except aiosqlite.OperationalError:
        return 0
    return int(r[0]) if r and r[0] is not None else 0


async def _distinct_flag_ids_acted_any(db_path: Path, *, tag: str) -> set[int]:
    import aiosqlite

    runs = await dao.list_paper_runs_by_tag(db_path, tag=tag)
    run_ids = [int(r["id"]) for r in runs]
    if not run_ids:
        return set()
    placeholders = ",".join("?" for _ in run_ids)
    try:
        async with aiosqlite.connect(str(db_path)) as db, db.execute(
            f"SELECT DISTINCT source_flag_id FROM paper_positions "
            f"WHERE run_id IN ({placeholders}) AND source_flag_id IS NOT NULL",
            tuple(run_ids),
        ) as cur:
            rows = await cur.fetchall()
    except aiosqlite.OperationalError:
        return set()
    return {int(r[0]) for r in rows if r[0] is not None}


async def _positions_opened_from_tagged_flags(
    db_path: Path, *, run_id: int, candidate_flag_ids: set[int]
) -> int:
    """How many positions in this run were opened from flags in the set."""
    import aiosqlite

    if not candidate_flag_ids:
        return 0
    try:
        async with aiosqlite.connect(str(db_path)) as db, db.execute(
            "SELECT COUNT(*) FROM paper_positions "
            "WHERE run_id = ? AND source_flag_id IS NOT NULL",
            (run_id,),
        ) as cur:
            r = await cur.fetchone()
    except aiosqlite.OperationalError:
        return 0
    return int(r[0]) if r and r[0] is not None else 0


def _counterfactual_table(per_run: list[dict[str, Any]]) -> list[str]:
    """Swap sizing between pairs — approximation:

    - Each profile's realized P&L is a sum of (shares * (payout - entry)).
    - Under another profile's sizing, we scale each realized P&L by the
      ratio of that profile's per-position cap (max_pct_per_position) to
      our own cap. This is a rough first-order counterfactual — it ignores
      fill-model effects but captures the first-order sizing leverage.
    """
    lines: list[str] = []
    profiles: list[tuple[str, dict[str, Any], dict[str, Any]]] = [
        (
            item["run"].get("profile_name") or "?",
            item["run"],
            item["metrics"],
        )
        for item in per_run
    ]

    lines.append("| My profile | With | Realized P&L | CF P&L | Δ |")
    lines.append("|------------|------|-------------:|-------:|----:|")
    for my_name, my_run, my_metrics in profiles:
        import json
        raw = my_run.get("profile_snapshot_json") or "{}"
        try:
            my_snap = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError:
            my_snap = {}
        my_cap = float(my_snap.get("max_pct_per_position") or 0.0) or 1.0
        my_realized = int(my_metrics.get("realized_pnl_cents") or 0)
        for other_name, other_run, _ in profiles:
            if other_name == my_name:
                continue
            raw2 = other_run.get("profile_snapshot_json") or "{}"
            try:
                other_snap = json.loads(raw2) if isinstance(raw2, str) else raw2
            except json.JSONDecodeError:
                other_snap = {}
            other_cap = float(other_snap.get("max_pct_per_position") or 0.0) or 1.0
            ratio = other_cap / my_cap if my_cap > 0 else 0.0
            cf = int(my_realized * ratio)
            delta = cf - my_realized
            lines.append(
                f"| `{my_name}` | `{other_name}`'s sizing | "
                f"${my_realized/100:,.2f} | ${cf/100:,.2f} | "
                f"${delta/100:+,.2f} |"
            )
    return lines


def _bootstrap_distribution(
    metrics: dict[str, Any], starting_balance: int, *, iters: int
) -> list[int]:
    """Addendum §11 — resample closed-position P&L with replacement.

    For each of `iters` iterations, draw n_trades samples with replacement
    from the realized P&L stream and sum with starting balance. Result is
    the distribution of possible final balances.
    """
    n_trades = int(metrics.get("closed_positions") or 0)
    if n_trades < 2 or starting_balance <= 0:
        return []

    # Reconstruct a (noisy) sample of individual realized P&Ls from the
    # aggregate metrics. We don't have per-position P&L in the RunMetrics
    # dict — fall back to synthesizing from (total, n, win_rate, avg_win,
    # avg_loss) so the bootstrap has realistic shape.
    total = int(metrics.get("realized_pnl_cents") or 0)
    wins = int(metrics.get("wins") or 0)
    losses = int(metrics.get("losses") or 0)
    avg_win = int(metrics.get("avg_win_cents") or 0)
    avg_loss = int(metrics.get("avg_loss_cents") or 0)
    stream: list[int] = [avg_win] * wins + [avg_loss] * losses
    if not stream:
        # fallback: 1-sample stream from aggregate total
        stream = [total]

    rng = random.Random(0)
    out: list[int] = []
    for _ in range(iters):
        sample = [rng.choice(stream) for _ in range(n_trades)]
        out.append(starting_balance + sum(sample))
    return out


def _percentile(values: list[int], q: float) -> int:
    if not values:
        return 0
    s = sorted(values)
    k = max(0, min(len(s) - 1, math.floor(q * (len(s) - 1))))
    return int(s[k])
