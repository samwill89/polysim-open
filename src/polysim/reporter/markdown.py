"""Markdown report generator — build plan §5.7.

Layout mirrors `polysim-demo-annotated.html` panel #5. Pure function of a
`RunMetrics` dict + the two baseline `TTestResult`s + optional calibration
PNG path + any kill-switch / anomaly notes.
"""

from __future__ import annotations

from typing import Any

from polysim.evaluator.calibration import ascii_plot, is_monotonic
from polysim.evaluator.metrics import (
    CalibrationBucket,
    DragByCategory,
    RunMetrics,
    WalletPnl,
)
from polysim.evaluator.significance import TTestResult


def render(
    metrics: RunMetrics,
    *,
    null_test: TTestResult | None = None,
    favorite_test: TTestResult | None = None,
    calibration_png_path: str | None = None,
    kill_switch_notes: list[str] | None = None,
    anomaly_notes: list[str] | None = None,
    investigator_lift: dict[str, Any] | None = None,
) -> str:
    lines: list[str] = []

    run_id = metrics.get("run_id", 0)
    run_name = metrics.get("run_name") or "(unnamed)"
    lines.append(f"# PolySim Run Report — run #{run_id} `{run_name}`")
    lines.append("")

    started = metrics.get("started_at") or "?"
    ended = metrics.get("ended_at") or "(ongoing)"
    window = metrics.get("window_days") or 0.0
    start_bal = int(metrics.get("starting_balance_cents") or 0)
    end_bal = int(metrics.get("current_balance_cents") or 0)
    pnl_total = int(metrics.get("total_pnl_cents") or 0)
    pnl_pct = float(metrics.get("net_return_pct") or 0.0) * 100

    lines.append(
        f"**Window:** {started} → {ended} ({window:.1f} days) &nbsp;·&nbsp; "
        f"**Starting balance:** ${start_bal/100:,.2f} &nbsp;·&nbsp; "
        f"**Final:** ${end_bal/100:,.2f} "
        f"({_signed_pct(pnl_pct)})"
    )
    lines.append("")

    # Verdict blockquote.
    verdicts: list[str] = []
    if null_test is not None:
        verdicts.append(
            f"null p={null_test.p_value:.3f} "
            f"({'PASS' if null_test.passes else 'FAIL'})"
        )
    if favorite_test is not None:
        verdicts.append(
            f"favorite p={favorite_test.p_value:.3f} "
            f"({'PASS' if favorite_test.passes else 'FAIL'})"
        )
    if verdicts:
        lines.append("> " + " · ".join(verdicts))
        lines.append("")

    # 1. Headline metrics.
    lines.append("## 1. Headline metrics")
    lines.append("")
    lines.append("| Metric | Value | Metric | Value |")
    lines.append("| --- | --- | --- | --- |")
    lines.append(
        f"| Total P&L | {_signed_money(pnl_total)} | "
        f"Win rate | {metrics.get('win_rate', 0)*100:.1f}% "
        f"({metrics.get('wins', 0)}/{metrics.get('closed_positions', 0)}) |"
    )
    lines.append(
        f"| Realized | {_signed_money(int(metrics.get('realized_pnl_cents') or 0))} | "
        f"Avg win / loss | {_signed_money(int(metrics.get('avg_win_cents') or 0))} / "
        f"{_signed_money(int(metrics.get('avg_loss_cents') or 0))} |"
    )
    lines.append(
        f"| Unrealized | {_signed_money(int(metrics.get('unrealized_pnl_cents') or 0))} | "
        f"Expectancy | {_signed_money(int(metrics.get('expectancy_cents') or 0))} |"
    )
    lines.append(
        f"| Sharpe (ann.) | {float(metrics.get('sharpe_annualized') or 0):.2f} | "
        f"Trades / day | {float(metrics.get('trades_per_day') or 0):.2f} |"
    )
    lines.append(
        f"| Sortino | {float(metrics.get('sortino_annualized') or 0):.2f} | "
        f"Avg holding | {float(metrics.get('avg_holding_hours') or 0):.1f}h |"
    )
    dd_pct = float(metrics.get('max_drawdown_pct') or 0) * 100
    recovery = metrics.get("max_drawdown_recovery_days")
    recovery_str = f"{recovery}d" if recovery is not None else "not recovered"
    lines.append(
        f"| Max drawdown | {dd_pct:.2f}% | "
        f"DD duration / recov | {int(metrics.get('max_drawdown_duration_days') or 0)}d / {recovery_str} |"
    )
    lines.append("")

    # 2. P&L by category.
    cat = metrics.get("pnl_by_category") or {}
    if cat:
        lines.append("## 2. P&L by category")
        lines.append("")
        lines.append("| Category | P&L |")
        lines.append("| --- | --- |")
        for c, v in sorted(cat.items(), key=lambda kv: -int(kv[1])):
            lines.append(f"| {c} | {_signed_money(int(v))} |")
        lines.append("")

    # 3. Detector attribution.
    det = metrics.get("pnl_by_detector") or {}
    if det:
        lines.append("## 3. Detector attribution (proportional)")
        lines.append("")
        total_attributed = sum(abs(v) for v in det.values()) or 1
        lines.append("| Detector | P&L | Share |")
        lines.append("| --- | --- | --- |")
        for d, v in sorted(det.items(), key=lambda kv: -int(kv[1])):
            share = (abs(int(v)) / total_attributed) * 100
            lines.append(f"| {d} | {_signed_money(int(v))} | {share:.1f}% |")
        lines.append("")

    # 4. Baseline comparisons.
    if null_test is not None or favorite_test is not None:
        lines.append("## 4. Baseline comparisons (paired t-test, alpha=0.05)")
        lines.append("")
        lines.append("| Baseline | t | p-value | n | Verdict |")
        lines.append("| --- | --- | --- | --- | --- |")
        if null_test is not None:
            lines.append(
                f"| Null (random direction) | {null_test.t_statistic:.2f} | "
                f"{null_test.p_value:.4f} | {null_test.n_samples} | "
                f"{'PASS' if null_test.passes else 'FAIL'} |"
            )
        if favorite_test is not None:
            lines.append(
                f"| Favorite-mid (always YES) | {favorite_test.t_statistic:.2f} | "
                f"{favorite_test.p_value:.4f} | {favorite_test.n_samples} | "
                f"{'PASS' if favorite_test.passes else 'FAIL'} |"
            )
        lines.append("")

    # 5. Top source wallets.
    top: list[WalletPnl] = metrics.get("pnl_by_source_wallet_top") or []
    bottom: list[WalletPnl] = metrics.get("pnl_by_source_wallet_bottom") or []
    if top or bottom:
        lines.append("## 5. Top source wallets")
        lines.append("")
        if top:
            lines.append("**Top contributors**")
            lines.append("")
            lines.append("| Wallet | P&L | Positions |")
            lines.append("| --- | --- | --- |")
            for w in top:
                lines.append(
                    f"| `{w['wallet'][:12]}...` | {_signed_money(int(w['pnl_cents']))} | "
                    f"{int(w['positions'])} |"
                )
            lines.append("")
        if bottom:
            lines.append("**Bottom contributors**")
            lines.append("")
            lines.append("| Wallet | P&L | Positions |")
            lines.append("| --- | --- | --- |")
            for w in bottom:
                lines.append(
                    f"| `{w['wallet'][:12]}...` | {_signed_money(int(w['pnl_cents']))} | "
                    f"{int(w['positions'])} |"
                )
            lines.append("")

    # 6. Execution drag.
    drag: list[DragByCategory] = metrics.get("execution_drag_by_category") or []
    if drag:
        lines.append("## 6. Execution drag")
        lines.append("")
        lines.append("Slippage cost (cents) per category — the edge we lost to latency.")
        lines.append("")
        lines.append("| Category | n | Mean drag | p95 drag | Total |")
        lines.append("| --- | --- | --- | --- | --- |")
        for dd in drag:
            mean_c = float(dd["mean_drag_cents"])
            p95_c = float(dd["p95_drag_cents"])
            total_c = int(dd["total_drag_cents"])
            lines.append(
                f"| {dd['category']} | {dd['n']} | "
                f"${mean_c/100:.4f} | ${p95_c/100:.4f} | ${total_c/100:.2f} |"
            )
        lines.append("")

    # 7. Calibration plot.
    cal: list[CalibrationBucket] = metrics.get("calibration_buckets") or []
    if cal:
        lines.append("## 7. Calibration")
        lines.append("")
        lines.append("Hit rate by composite-score bucket. Expect monotonic rise.")
        lines.append("")
        lines.append("```")
        lines.append(ascii_plot(cal))
        lines.append("```")
        lines.append(
            "Monotonicity: "
            + ("✓" if is_monotonic(cal) else "✗ — review high-score buckets")
        )
        if calibration_png_path:
            lines.append(f"\n![calibration]({calibration_png_path})")
        lines.append("")

    # 8. Invalid markets.
    invalid_n = int(metrics.get("invalid_markets") or 0)
    invalid_pct = float(metrics.get("invalid_market_pct") or 0) * 100
    closed_n = int(metrics.get("closed_positions") or 0)
    lines.append("## 8. Invalid / disputed markets")
    lines.append("")
    lines.append(
        f"{invalid_n}/{closed_n} closed positions ({invalid_pct:.1f}%) in INVALID markets. "
        + ("**PASS** (<10%)." if invalid_pct < 10.0 else "**WARN** (>10%).")
    )
    lines.append("")

    # 9. Kill-switch activity.
    if kill_switch_notes:
        lines.append("## 9. Kill-switch activity")
        lines.append("")
        for note in kill_switch_notes:
            lines.append(f"- {note}")
        lines.append("")

    # 10. Investigator lift (optional).
    if investigator_lift:
        lines.append("## 10. Investigator lift")
        lines.append("")
        for k, v in investigator_lift.items():
            lines.append(f"- **{k}**: {v}")
        lines.append("")

    # 11. Anomalies & notes.
    if anomaly_notes:
        lines.append("## 11. Anomalies & operator notes")
        lines.append("")
        for note in anomaly_notes:
            lines.append(f"- {note}")
        lines.append("")

    return "\n".join(lines)


# ── formatting helpers ──────────────────────────────────


def _signed_money(cents: int) -> str:
    dollars = cents / 100.0
    if dollars >= 0:
        return f"+${dollars:,.2f}"
    return f"-${-dollars:,.2f}"


def _signed_pct(pct: float) -> str:
    return f"{'+' if pct >= 0 else ''}{pct:.2f}%"
