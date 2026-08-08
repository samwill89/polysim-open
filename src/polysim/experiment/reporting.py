"""End-of-experiment reporting — empirical-priors addendum §8.5.

Reads from the experiment DB (paper_runs, paper_positions, decision_rejections,
wallets_discovery) plus the JSONL belief / rejection logs and produces:

  * `gather_experiment_data()` — pulls every input H1..H6 needs in one pass.
  * `render_markdown()`        — formats a verdict table + per-hypothesis
                                 detail block, mirroring the §8.5 layout.

The renderer never trains anything, never re-runs the simulator. It only
*describes* what already happened. If a hypothesis lacks data, its row reads
"insufficient sample" and the overall report is still emitted (so an operator
can ship a 72-hour interim with H1+H6 populated and the rest pending).

We also surface a "decision-gate funnel" table because the H6 mechanism
(judgment-layer veto) is the most actionable signal for an operator.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

from polysim.experiment.hypotheses import (
    TestResult,
    h1_systematic_returns_positive,
    h2_degen_higher_mean,
    h3_niche_outperforms_general,
    h4_edge_likelihood_predicts_pnl,
    h5_lunar_top_pnl_predicts_forward,
    h6_investigator_judgment_adds_value,
)

log = logging.getLogger(__name__)

DEFAULT_REJECTIONS_LOG = Path("logs/decisions/rejections.jsonl")
DEFAULT_BELIEFS_DIR = Path("logs/beliefs")


# ── Data container ──────────────────────────────────────


@dataclass(frozen=True)
class ExperimentSummary:
    """Counts + headline stats for the experiment."""

    experiment_id: int
    name: str
    cohort_size: int
    cohort_hash: str | None
    started_at: str
    ended_at: str | None
    paper_runs: int
    paper_positions_open: int
    paper_positions_closed: int
    paper_positions_resolved: int
    realized_pnl_cents: int
    rejections_total: int
    rejections_by_gate: dict[str, int]


@dataclass(frozen=True)
class HypothesisInputs:
    """Aligned arrays for H1..H6. Empty-list arrays trigger the
    "insufficient sample" branch in each hypothesis function."""

    daily_returns_systematic: list[float] = field(default_factory=list)
    daily_returns_degen: list[float] = field(default_factory=list)
    pnl_niche: list[float] = field(default_factory=list)
    pnl_general: list[float] = field(default_factory=list)
    edge_likelihoods: list[float] = field(default_factory=list)
    forward_pnl: list[float] = field(default_factory=list)
    historical_pnl: list[float] = field(default_factory=list)
    forward_pnl_for_lunar: list[float] = field(default_factory=list)
    pnl_passed_gates: list[float] = field(default_factory=list)
    pnl_vetoed_counterfactual: list[float] = field(default_factory=list)


@dataclass(frozen=True)
class ExperimentData:
    """Everything render_markdown() needs to format the report."""

    summary: ExperimentSummary
    inputs: HypothesisInputs


# ── Gather phase ────────────────────────────────────────


async def gather_experiment_data(
    db_path: Path,
    *,
    experiment_id: int | None = None,
    rejections_log: Path = DEFAULT_REJECTIONS_LOG,
) -> ExperimentData:
    """One-pass read of everything reporting needs.

    If `experiment_id` is None, picks the most recent experiment row.
    Returns a frozen `ExperimentData` so the renderer can be a pure
    function of it.
    """
    async with aiosqlite.connect(str(db_path)) as db:
        db.row_factory = aiosqlite.Row
        experiment_row = await _load_experiment(db, experiment_id)
        if experiment_row is None:
            raise RuntimeError(
                "No experiments found — run `polysim discovery run` first."
            )

        runs = await _load_runs(db)
        positions = await _load_positions(db)
        wallets_by_addr = await _load_wallets(db)

    rejections = _read_rejections(rejections_log)

    summary = _build_summary(experiment_row, runs, positions, rejections)
    inputs = _build_inputs(runs, positions, wallets_by_addr, rejections)
    return ExperimentData(summary=summary, inputs=inputs)


async def _load_experiment(
    db: aiosqlite.Connection, experiment_id: int | None
) -> aiosqlite.Row | None:
    if experiment_id is not None:
        async with db.execute(
            "SELECT * FROM experiments WHERE id = ?", (experiment_id,)
        ) as cur:
            return await cur.fetchone()
    async with db.execute(
        "SELECT * FROM experiments ORDER BY id DESC LIMIT 1"
    ) as cur:
        return await cur.fetchone()


async def _load_runs(db: aiosqlite.Connection) -> list[dict[str, Any]]:
    async with db.execute(
        "SELECT id, name, profile_name, tag, started_at, ended_at, "
        "starting_balance_cents, current_balance_cents "
        "FROM paper_runs"
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]


async def _load_positions(db: aiosqlite.Connection) -> list[dict[str, Any]]:
    async with db.execute(
        "SELECT id, run_id, market_id, status, size_shares, avg_entry_price_cents, "
        "opened_at, closed_at, realized_pnl_cents, source_wallet "
        "FROM paper_positions"
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]


async def _load_wallets(
    db: aiosqlite.Connection,
) -> dict[str, dict[str, Any]]:
    async with db.execute(
        "SELECT address, cohort_niche, edge_likelihood_global, "
        "edge_likelihood_aec, edge_likelihood_ai_labs, "
        "edge_likelihood_creator_econ "
        "FROM wallets_discovery WHERE is_cohort = 1"
    ) as cur:
        rows = await cur.fetchall()
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        d = dict(r)
        # Address may be stored case-canonicalized; keep as-is plus lower form.
        addr = str(d["address"])
        out[addr] = d
        out[addr.lower()] = d
    return out


def _read_rejections(path: Path) -> list[dict[str, Any]]:
    """Best-effort parse of `logs/decisions/rejections.jsonl`."""
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                log.warning("malformed rejection log line skipped")
    return rows


# ── Build phase ─────────────────────────────────────────


def _build_summary(
    exp: aiosqlite.Row,
    runs: list[dict[str, Any]],
    positions: list[dict[str, Any]],
    rejections: list[dict[str, Any]],
) -> ExperimentSummary:
    by_status: dict[str, int] = defaultdict(int)
    realized_total = 0
    for p in positions:
        by_status[str(p["status"])] += 1
        if p["realized_pnl_cents"] is not None:
            realized_total += int(p["realized_pnl_cents"])
    by_gate: dict[str, int] = defaultdict(int)
    for r in rejections:
        # Belief schema stamps gate verdicts; rejection log records first failure.
        gate = str(r.get("first_failure") or r.get("gate") or "unknown")
        by_gate[gate] += 1
    return ExperimentSummary(
        experiment_id=int(exp["id"]),
        name=str(exp["name"]),
        cohort_size=int(exp["cohort_size"] or 0),
        cohort_hash=exp["cohort_hash"],
        started_at=str(exp["started_at"]),
        ended_at=exp["ended_at"],
        paper_runs=len(runs),
        paper_positions_open=by_status.get("OPEN", 0),
        paper_positions_closed=by_status.get("CLOSED", 0),
        paper_positions_resolved=by_status.get("RESOLVED", 0),
        realized_pnl_cents=realized_total,
        rejections_total=len(rejections),
        rejections_by_gate=dict(by_gate),
    )


def _daily_returns_for_runs(
    runs: list[dict[str, Any]], positions: list[dict[str, Any]],
    *, profile: str,
) -> list[float]:
    """Group P&L by day for runs matching `profile`. Returns return-pct list."""
    run_ids = {r["id"] for r in runs if r.get("profile_name") == profile}
    if not run_ids:
        return []
    starting_by_run = {r["id"]: max(int(r["starting_balance_cents"] or 1), 1)
                       for r in runs if r["id"] in run_ids}
    by_day: dict[tuple[int, str], int] = defaultdict(int)
    for p in positions:
        if p["run_id"] not in run_ids:
            continue
        if p["realized_pnl_cents"] is None:
            continue
        ts = p["closed_at"] or p["opened_at"]
        if not ts:
            continue
        day = str(ts)[:10]
        by_day[(int(p["run_id"]), day)] += int(p["realized_pnl_cents"])
    returns: list[float] = []
    for (run_id, _day), pnl in by_day.items():
        returns.append(pnl / starting_by_run[run_id])
    return returns


def _classify_niche(
    wallet: str | None, wallets_by_addr: dict[str, dict[str, Any]]
) -> str:
    if not wallet:
        return "unknown"
    info = wallets_by_addr.get(wallet) or wallets_by_addr.get(wallet.lower())
    if not info:
        return "unknown"
    niche = info.get("cohort_niche")
    return str(niche) if niche else "general"


def _edge_likelihood(
    wallet: str | None, wallets_by_addr: dict[str, dict[str, Any]]
) -> float | None:
    if not wallet:
        return None
    info = wallets_by_addr.get(wallet) or wallets_by_addr.get(wallet.lower())
    if not info:
        return None
    return info.get("edge_likelihood_global")


def _build_inputs(
    runs: list[dict[str, Any]],
    positions: list[dict[str, Any]],
    wallets_by_addr: dict[str, dict[str, Any]],
    rejections: list[dict[str, Any]],
) -> HypothesisInputs:
    daily_sys = _daily_returns_for_runs(runs, positions, profile="systematic")
    daily_deg = _daily_returns_for_runs(runs, positions, profile="degen")

    closed = [p for p in positions
              if p["realized_pnl_cents"] is not None and p["status"] != "OPEN"]

    pnl_niche: list[float] = []
    pnl_general: list[float] = []
    for p in closed:
        niche = _classify_niche(p.get("source_wallet"), wallets_by_addr)
        pnl = float(p["realized_pnl_cents"])
        if niche in ("aec", "ai_labs", "creator_econ"):
            pnl_niche.append(pnl)
        elif niche == "general":
            pnl_general.append(pnl)

    edge_likelihoods: list[float] = []
    forward_pnl: list[float] = []
    for p in closed:
        el = _edge_likelihood(p.get("source_wallet"), wallets_by_addr)
        if el is None:
            continue
        edge_likelihoods.append(float(el))
        forward_pnl.append(float(p["realized_pnl_cents"]))

    # H5 (Lunar replay) is a backfill artifact — populated only if a Lunar
    # pre-experiment snapshot exists. Default to empty.
    historical_pnl: list[float] = []
    forward_pnl_for_lunar: list[float] = []

    pnl_passed_gates = [float(p["realized_pnl_cents"]) for p in closed]
    pnl_vetoed_counterfactual = [
        float(r["counterfactual_pnl_cents"])
        for r in rejections
        if r.get("counterfactual_pnl_cents") is not None
    ]

    return HypothesisInputs(
        daily_returns_systematic=daily_sys,
        daily_returns_degen=daily_deg,
        pnl_niche=pnl_niche,
        pnl_general=pnl_general,
        edge_likelihoods=edge_likelihoods,
        forward_pnl=forward_pnl,
        historical_pnl=historical_pnl,
        forward_pnl_for_lunar=forward_pnl_for_lunar,
        pnl_passed_gates=pnl_passed_gates,
        pnl_vetoed_counterfactual=pnl_vetoed_counterfactual,
    )


# ── Run hypotheses ──────────────────────────────────────


def run_all_hypotheses(data: ExperimentData) -> list[TestResult]:
    """Execute H1..H6 against the gathered inputs. Order is preserved."""
    i = data.inputs
    return [
        h1_systematic_returns_positive(i.daily_returns_systematic),
        h2_degen_higher_mean(i.daily_returns_systematic, i.daily_returns_degen),
        h3_niche_outperforms_general(i.pnl_niche, i.pnl_general),
        h4_edge_likelihood_predicts_pnl(i.edge_likelihoods, i.forward_pnl),
        h5_lunar_top_pnl_predicts_forward(
            i.historical_pnl, i.forward_pnl_for_lunar
        ),
        h6_investigator_judgment_adds_value(
            i.pnl_passed_gates, i.pnl_vetoed_counterfactual
        ),
    ]


# ── Render phase ────────────────────────────────────────


_TITLE_BY_HYPOTHESIS = {
    "H1": "Systematic returns > 0 net of bid-ask",
    "H2": "Degen mean > systematic mean",
    "H3": "Niche cohort outperforms general",
    "H4": "edge_likelihood predicts forward P&L (rho>0.3)",
    "H5": "Lunar: top historical P&L predicts forward",
    "H6": "Judgment layer (gates) adds value",
}


def _verdict_emoji(verdict: str) -> str:
    return {
        "accept_alt": "ACCEPT",
        "reject_alt": "REJECT",
        "ambiguous": "AMBIG",
    }.get(verdict, "?")


def render_markdown(
    data: ExperimentData,
    results: list[TestResult],
    *,
    rendered_at: datetime | None = None,
) -> str:
    """Format the §8.5 end-of-experiment report. Pure function."""
    rendered_at = rendered_at or datetime.now(UTC)
    s = data.summary

    lines: list[str] = []
    lines.append(
        f"# Experiment Report — `{s.name}` (id #{s.experiment_id})"
    )
    lines.append("")
    lines.append(
        f"**Cohort:** {s.cohort_size} wallets &nbsp;·&nbsp; "
        f"**Hash:** `{(s.cohort_hash or '-')[:16]}...` &nbsp;·&nbsp; "
        f"**Started:** {s.started_at[:19]} &nbsp;·&nbsp; "
        f"**Ended:** {(s.ended_at or '(ongoing)')[:19]}"
    )
    lines.append(
        f"**Paper runs:** {s.paper_runs} &nbsp;·&nbsp; "
        f"**Positions open/closed/resolved:** "
        f"{s.paper_positions_open}/{s.paper_positions_closed}/{s.paper_positions_resolved}"
        f" &nbsp;·&nbsp; **Realized P&L:** ${s.realized_pnl_cents/100:+,.2f}"
    )
    lines.append(
        f"**Rejected trades:** {s.rejections_total} "
        f"_(rendered {rendered_at.isoformat()})_"
    )
    lines.append("")

    # ── Verdict table ──
    lines.append("## Pre-registered hypotheses")
    lines.append("")
    lines.append(
        "| ID | Hypothesis | n | statistic | p | effect | verdict |"
    )
    lines.append("| --- | --- | ---: | ---: | ---: | ---: | --- |")
    for r in results:
        title = _TITLE_BY_HYPOTHESIS.get(r.hypothesis_id, r.null)
        stat = (
            f"{r.statistic:.3f}" if r.statistic == r.statistic  # NaN check
            else "-"
        )
        lines.append(
            f"| {r.hypothesis_id} | {title} | {r.n} | {stat} | "
            f"{r.p_value:.3f} | {r.effect_size:.3f} | "
            f"**{_verdict_emoji(r.verdict)}** |"
        )
    lines.append("")

    # ── Per-hypothesis detail ──
    lines.append("## Detail")
    lines.append("")
    for r in results:
        lines.append(f"### {r.hypothesis_id} — {_TITLE_BY_HYPOTHESIS.get(r.hypothesis_id, '')}")
        lines.append("")
        lines.append(f"- **Null:** {r.null}")
        lines.append(f"- **alpha:** {r.alpha}  &nbsp;·&nbsp; **n:** {r.n}")
        if r.notes:
            lines.append(f"- **Notes:** {r.notes}")
        lines.append(f"- **Verdict:** `{r.verdict}` (effect_size={r.effect_size:.3f})")
        lines.append("")

    # ── Decision-gate funnel ──
    lines.append("## Decision-gate funnel")
    lines.append("")
    if not s.rejections_by_gate:
        lines.append("_No rejections logged yet._")
    else:
        lines.append("| First-failed gate | Count |")
        lines.append("| --- | ---: |")
        for gate, n in sorted(
            s.rejections_by_gate.items(), key=lambda kv: -kv[1]
        ):
            lines.append(f"| `{gate}` | {n} |")
    lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(
        "_Report generated by `polysim experiment report`. "
        "Verdicts treat p<alpha as accept_alt, p>0.5 as reject_alt, "
        "and the gap as ambiguous (per §8.5)._"
    )
    lines.append("")
    return "\n".join(lines)


__all__ = [
    "DEFAULT_BELIEFS_DIR",
    "DEFAULT_REJECTIONS_LOG",
    "ExperimentData",
    "ExperimentSummary",
    "HypothesisInputs",
    "gather_experiment_data",
    "render_markdown",
    "run_all_hypotheses",
]
