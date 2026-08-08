"""Calibration — composite score bucket vs hit rate.

Build plan §5.6. Consumes the `calibration_buckets` slice produced by
metrics.py; renders ASCII bars for the terminal and (optionally) a PNG
via matplotlib when available.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from polysim.evaluator.metrics import CalibrationBucket

log = logging.getLogger(__name__)


def ascii_plot(
    buckets: Sequence[CalibrationBucket],
    *,
    bar_width: int = 30,
) -> str:
    """ASCII horizontal bar chart — used in CLI report."""
    if not buckets:
        return "  (no closed positions with composite score)"
    lines = [
        f"  {'score':<12}  {'n':>4}  {'hit%':>5}  bar",
        f"  {'-' * 12}  {'-' * 4}  {'-' * 5}  " + "-" * bar_width,
    ]
    for b in buckets:
        filled = round(b["hit_rate"] * bar_width)
        bar = "#" * filled + "." * (bar_width - filled)
        lines.append(
            f"  {b['range']:<12}  {b['n']:>4}  {b['hit_rate']*100:>4.0f}%  {bar}"
        )
    return "\n".join(lines)


def is_monotonic(buckets: Sequence[CalibrationBucket]) -> bool:
    """Hit rate should weakly increase as composite score rises."""
    if len(buckets) < 2:
        return True
    prev = buckets[0]["hit_rate"]
    for b in buckets[1:]:
        if b["hit_rate"] < prev - 1e-9:
            return False
        prev = b["hit_rate"]
    return True


def brier_score(
    buckets: Sequence[CalibrationBucket],
    *,
    bin_center_offset: float = 0.25,
    score_to_prob: float = 10.0,
) -> float:
    """Approximate Brier score over the bucket-binned predictions.

    Each bucket's nominal predicted probability is `(lo + offset) / score_to_prob`
    (e.g., bucket 7.0-7.5 -> 0.725). Returns mean squared error vs hit_rate,
    weighted by bucket count.
    """
    if not buckets:
        return 0.0
    total_n = sum(b["n"] for b in buckets)
    if total_n == 0:
        return 0.0
    total_sq_err = 0.0
    for b in buckets:
        lo = float(b["range"].split("-")[0])
        predicted = min(1.0, (lo + bin_center_offset) / score_to_prob)
        err = predicted - b["hit_rate"]
        total_sq_err += (err * err) * b["n"]
    return total_sq_err / total_n


def save_png(
    buckets: Sequence[CalibrationBucket], output_path: Path
) -> bool:
    """Write a matplotlib PNG. Returns False silently if matplotlib missing."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        log.info("matplotlib not installed; skipping PNG output")
        return False
    if not buckets:
        return False
    xs = [b["range"] for b in buckets]
    hit_rates = [b["hit_rate"] for b in buckets]
    ns = [b["n"] for b in buckets]
    fig, ax = plt.subplots(figsize=(8, 4))
    bars = ax.bar(xs, hit_rates, color="#58a6ff")
    for bar, n in zip(bars, ns, strict=False):
        ax.annotate(
            f"n={n}",
            xy=(bar.get_x() + bar.get_width() / 2, bar.get_height()),
            xytext=(0, 3),
            textcoords="offset points",
            ha="center",
            fontsize=8,
        )
    ax.set_ylim(0, 1)
    ax.set_ylabel("Hit rate")
    ax.set_xlabel("Composite score bucket")
    ax.set_title("Calibration — composite score vs realized hit rate")
    fig.tight_layout()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, dpi=150)
    plt.close(fig)
    return True


# Convenience pass-through so callers only need one import.
def summary(
    buckets: Sequence[CalibrationBucket],
) -> dict[str, Any]:
    return {
        "buckets": list(buckets),
        "ascii": ascii_plot(buckets),
        "monotonic": is_monotonic(buckets),
        "brier": brier_score(buckets),
    }
