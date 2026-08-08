"""Measure investigator lift — Phase 3 build-plan §3.8.

Reads composite-raised flags from the local SQLite DB, partitions them
into known-insider (TP set) vs everything-else (FP set) using the
addresses pinned in config.known_insiders[], then invokes the Claude
investigator on each and reports:

  - TP retention = fraction of TP flags the investigator labels INFORMED
                   at confidence >= min_verdict_confidence_to_act.
  - FP reduction = fraction of FP flags where the investigator decides
                   NOT to act (verdict != INFORMED or low confidence).
  - Total cost  = sum of flag_costs for runs in this window.

CAVEAT on FP set: a flag outside the known_insiders[] set is not
necessarily a false positive — it may be an as-yet-undiscovered insider.
Treat FP-reduction as an upper bound on conservatism, not a hard metric.

Usage:
    python scripts/measure_investigator_lift.py --db polysim.db \\
        --config config.yml --since 24h --limit 200
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from polysim.config import load_config, load_secrets
from polysim.db import dao
from polysim.investigator.agent import Investigator, investigate_flag
from polysim.utils.time import iso, parse_since


@dataclass
class FlagOutcomeStat:
    flag_id: int
    wallet: str
    composite: float
    is_known_insider: bool
    verdict: str | None
    confidence: float | None
    would_act: bool


@dataclass
class LiftResult:
    tp_total: int
    tp_informed: int
    fp_total: int
    fp_would_act: int
    by_flag: list[FlagOutcomeStat]
    cost_cents: int
    calls: int
    cached_tokens: int

    @property
    def tp_retention(self) -> float:
        return self.tp_informed / self.tp_total if self.tp_total else 0.0

    @property
    def fp_reduction(self) -> float:
        if self.fp_total == 0:
            return 0.0
        fp_kept = self.fp_would_act
        return 1.0 - (fp_kept / self.fp_total)


async def run(
    *,
    db_path: Path,
    config_path: Path,
    since_iso: str,
    limit: int,
    dry_run: bool,
) -> LiftResult:
    cfg = load_config(config_path)
    secrets = load_secrets()
    known = {k.address.lower() for k in cfg.known_insiders if k.address}

    inv = Investigator(
        api_key=secrets.ANTHROPIC_API_KEY or "",
        model=cfg.investigator.model,
        triage_model=cfg.investigator.triage_model,
        max_calls_per_day=cfg.investigator.max_calls_per_day,
        min_verdict_confidence_to_act=cfg.investigator.min_verdict_confidence_to_act,
    )

    flags = await dao.list_flags_since(
        db_path, since_iso=since_iso, category=None, limit=limit
    )
    outcomes: list[FlagOutcomeStat] = []
    for f in flags:
        wallet = str(f["wallet_address"]).lower()
        composite = float(f.get("composite_score") or 0.0)
        is_known = wallet in known

        if dry_run:
            outcomes.append(
                FlagOutcomeStat(
                    flag_id=int(f["id"]),
                    wallet=wallet,
                    composite=composite,
                    is_known_insider=is_known,
                    verdict=f.get("investigator_verdict"),
                    confidence=None,
                    would_act=bool(f.get("acted_on")),
                )
            )
            continue

        verdict = await investigate_flag(db_path, inv, int(f["id"]))
        if verdict is None:
            # cap hit or missing inputs — skip
            outcomes.append(
                FlagOutcomeStat(
                    flag_id=int(f["id"]),
                    wallet=wallet,
                    composite=composite,
                    is_known_insider=is_known,
                    verdict=None,
                    confidence=None,
                    would_act=False,
                )
            )
            continue
        outcomes.append(
            FlagOutcomeStat(
                flag_id=int(f["id"]),
                wallet=wallet,
                composite=composite,
                is_known_insider=is_known,
                verdict=verdict.verdict,
                confidence=verdict.confidence,
                would_act=inv.should_act(verdict),
            )
        )

    tp = [o for o in outcomes if o.is_known_insider]
    fp = [o for o in outcomes if not o.is_known_insider]
    tp_informed = sum(1 for o in tp if o.verdict == "INFORMED" and o.would_act)
    fp_would_act = sum(1 for o in fp if o.would_act)

    cost_summary = await dao.sum_flag_costs_since(db_path, since_iso=since_iso)
    return LiftResult(
        tp_total=len(tp),
        tp_informed=tp_informed,
        fp_total=len(fp),
        fp_would_act=fp_would_act,
        by_flag=outcomes,
        cost_cents=cost_summary["total_cost_cents"],
        calls=cost_summary["total_calls"],
        cached_tokens=cost_summary["total_cached_tokens"],
    )


def render_report(result: LiftResult) -> str:
    lines: list[str] = []
    lines.append("# Investigator lift — measurement report")
    lines.append("")
    lines.append(f"- Flags measured: **{len(result.by_flag)}**")
    lines.append(f"- Known-insider flags (TP): **{result.tp_total}**")
    lines.append(f"- Other flags (treated as FP): **{result.fp_total}**")
    lines.append("")
    lines.append("## Phase 3 acceptance")
    lines.append("")
    lines.append(
        f"- **TP retention**: {result.tp_retention:.1%}"
        f"  (target: >= 70% INFORMED on known insiders)"
    )
    lines.append(
        f"- **FP reduction**: {result.fp_reduction:.1%}"
        f"  (target: >= 30% of non-known flags dropped)"
    )
    lines.append("")
    lines.append("## Cost")
    lines.append("")
    lines.append(f"- Total API cost: ${result.cost_cents / 100:.4f}")
    lines.append(f"- Calls logged: {result.calls}")
    lines.append(f"- Cached tokens: {result.cached_tokens:,}")
    lines.append("")
    lines.append("## Per-flag detail")
    lines.append("")
    lines.append("| Flag | Wallet | Comp. | KnownIns | Verdict | Conf | Act |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for o in result.by_flag:
        lines.append(
            f"| {o.flag_id} | {o.wallet[:10]}... | {o.composite:.2f} | "
            f"{'y' if o.is_known_insider else ''} | "
            f"{o.verdict or '-'} | {o.confidence or 0:.2f} | "
            f"{'y' if o.would_act else ''} |"
        )
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=Path("polysim.db"))
    ap.add_argument("--config", type=Path, default=Path("config.yml"))
    ap.add_argument(
        "--since", type=str, default="7d",
        help="Time window looking back from now (e.g. 24h, 7d).",
    )
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument(
        "--dry-run", action="store_true",
        help="Read existing verdicts from DB; do not call the Anthropic API.",
    )
    ap.add_argument(
        "--output", type=Path, default=None,
        help="Write markdown report to this path (default: stdout).",
    )
    ap.add_argument(
        "--json-output", type=Path, default=None,
        help="Additionally write structured JSON to this path.",
    )
    args = ap.parse_args()

    delta = parse_since(args.since)
    since = datetime.now(UTC) - delta

    try:
        result = asyncio.run(
            run(
                db_path=args.db,
                config_path=args.config,
                since_iso=iso(since),
                limit=args.limit,
                dry_run=args.dry_run,
            )
        )
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    report = render_report(result)
    if args.output is None:
        print(report)
    else:
        args.output.write_text(report, encoding="utf-8")
        print(f"wrote {args.output}")

    if args.json_output is not None:
        payload = {
            "tp_total": result.tp_total,
            "tp_informed": result.tp_informed,
            "tp_retention": result.tp_retention,
            "fp_total": result.fp_total,
            "fp_would_act": result.fp_would_act,
            "fp_reduction": result.fp_reduction,
            "cost_cents": result.cost_cents,
            "calls": result.calls,
            "cached_tokens": result.cached_tokens,
        }
        args.json_output.write_text(
            json.dumps(payload, indent=2), encoding="utf-8"
        )

    # Exit code: 0 green, 1 if a target isn't met.
    targets_hit = (
        (result.tp_total == 0 or result.tp_retention >= 0.70)
        and (result.fp_total == 0 or result.fp_reduction >= 0.30)
    )
    return 0 if targets_hit else 1


if __name__ == "__main__":
    sys.exit(main())
