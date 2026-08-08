"""Paper-run manager — glues config + executor + DB for the CLI.

Build plan §4.6 (config snapshot) + §4.7 (CLI surface).

Lifecycle:
  start_run(cfg) -> run_id
    * validate config, snapshot to DB + reports/run_config.<id>.json
    * create paper_run row with starting_balance

  process_pending_flags(run_id, since) -> count
    * find flags since `since` with composite >= threshold AND
      (no investigator OR INFORMED+acted_on)
    * call executor.on_flag for each

  resolve_newly_closed_markets(run_id) -> count
    * find markets with resolved_outcome set whose OPEN positions remain

  stop_run(run_id)
    * mark ended_at
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from pathlib import Path
from typing import Any

from polysim.config import Config, RiskProfile
from polysim.db import dao
from polysim.paper.executor import PaperExecutor
from polysim.paper.fill_model import FillModel
from polysim.utils.time import iso, now_utc, parse_iso

log = logging.getLogger(__name__)


def _fill_model_from_config(cfg: Config) -> FillModel:
    fm = cfg.fill_model
    return FillModel(
        detection_latency_p50_ms=fm.detection_latency_p50_ms,
        detection_latency_p95_ms=fm.detection_latency_p95_ms,
        decision_latency_p50_ms=fm.decision_latency_p50_ms,
        decision_latency_p95_ms=fm.decision_latency_p95_ms,
        slippage_ticks=fm.slippage_ticks,
        on_partial=fm.on_partial,
        fee_bps=fm.fee_bps,
        historical_pessimism_multiplier=fm.historical_pessimism_multiplier,
    )


async def start_run(
    db_path: Path,
    cfg: Config,
    *,
    reports_dir: Path = Path("./reports"),
    profile: RiskProfile | None = None,
    name_override: str | None = None,
    balance_override_cents: int | None = None,
    tag: str | None = None,
) -> int:
    """Create a paper_run + write config snapshot. Returns new run id.

    When `profile` is given (addendum), persists profile_name + snapshot and
    uses `name_override` and `balance_override_cents` if set.
    """
    config_snapshot = cfg.model_dump()
    name = name_override or cfg.run.name
    balance = (
        balance_override_cents
        if balance_override_cents is not None
        else cfg.run.starting_balance_cents
    )
    profile_name = profile.name if profile else "systematic"
    profile_snapshot = profile.model_dump() if profile else None
    run_id = await dao.create_paper_run(
        db_path,
        name=name,
        starting_balance_cents=balance,
        config_snapshot=config_snapshot,
        profile_name=profile_name,
        profile_snapshot=profile_snapshot,
        tag=tag,
    )
    # Spec §14 #4 — snapshot config JSON to disk as well, so results are
    # reproducible even if the DB is later restored from a different source.
    reports_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path = reports_dir / f"run_config.{run_id}.json"
    combined: dict[str, Any] = {"config": config_snapshot}
    if profile_snapshot is not None:
        combined["profile"] = profile_snapshot
    if tag is not None:
        combined["tag"] = tag
    snapshot_path.write_text(
        json.dumps(combined, indent=2, default=str),
        encoding="utf-8",
    )
    log.info(
        "run #%d started  name=%s  profile=%s  balance=%s  tag=%s  config=%s",
        run_id, name, profile_name, balance, tag, snapshot_path,
    )
    return run_id


async def stop_run(
    db_path: Path,
    run_id: int,
    *,
    notes: str | None = None,
) -> None:
    await dao.end_paper_run(db_path, run_id, notes=notes)


async def process_pending_flags(
    db_path: Path,
    cfg: Config,
    *,
    run_id: int,
    since_seconds: int = 300,
    limit: int = 500,
) -> int:
    """Consume any flags since (now - since_seconds) not yet acted on by this run.

    Returns count of positions opened.
    """
    since_iso_ = iso(now_utc() - timedelta(seconds=since_seconds))
    flags = await dao.list_flags_since(
        db_path, since_iso=since_iso_, category=None, limit=limit
    )
    executor = PaperExecutor(
        db_path,
        run_id=run_id,
        bankroll=cfg.bankroll,
        fill_model=_fill_model_from_config(cfg),
    )
    opened = 0
    # Respect min_composite_to_invoke as a secondary gate on paper execution.
    for f in flags:
        comp = float(f.get("composite_score") or 0.0)
        if comp < cfg.scoring.flag_threshold:
            continue
        verdict = f.get("investigator_verdict")
        # If investigator ran: only act on INFORMED+acted_on. Otherwise (verdict
        # is NULL) the composite already passed the scorer's gate.
        if verdict is not None and verdict != "INFORMED":
            continue
        if verdict == "INFORMED" and not bool(f.get("acted_on")):
            continue
        pid = await executor.on_flag(int(f["id"]))
        if pid is not None:
            opened += 1
    return opened


async def resolve_closed_markets(
    db_path: Path,
    cfg: Config,
    *,
    run_id: int,
) -> int:
    """Close any OPEN positions whose markets have resolved. Returns count closed."""
    executor = PaperExecutor(
        db_path,
        run_id=run_id,
        bankroll=cfg.bankroll,
        fill_model=_fill_model_from_config(cfg),
    )
    positions = await dao.list_open_positions(db_path, run_id)
    seen_markets: set[str] = set()
    closed = 0
    for pos in positions:
        market_id = str(pos.get("market_id") or "")
        if market_id in seen_markets:
            continue
        seen_markets.add(market_id)
        market = await dao.get_market(db_path, market_id)
        if market is None or market.resolved_outcome is None:
            continue
        ids = await executor.on_resolution(market_id)
        closed += len(ids)
    return closed


async def run_status(
    db_path: Path, run_id: int
) -> dict[str, Any]:
    """Snapshot for `polysim run status`."""
    row = await dao.get_paper_run(db_path, run_id)
    if row is None:
        return {"error": f"run #{run_id} not found"}

    start = int(row.get("starting_balance_cents") or 0)
    current = int(row.get("current_balance_cents") or 0)
    started = parse_iso(str(row["started_at"])) if row.get("started_at") else None
    ended = parse_iso(str(row["ended_at"])) if row.get("ended_at") else None
    paused_at = row.get("paused_at")

    open_positions = await dao.list_open_positions(db_path, run_id)
    open_notional = sum(
        int(p.get("size_shares") or 0) * int(p.get("avg_entry_price_cents") or 0)
        for p in open_positions
    )

    realized = sum(
        int(p.get("realized_pnl_cents") or 0)
        for p in await _list_closed_positions(db_path, run_id)
    )

    return {
        "id": run_id,
        "name": row.get("name"),
        "started_at": started.isoformat() if started else None,
        "ended_at": ended.isoformat() if ended else None,
        "paused": bool(paused_at),
        "paused_reason": row.get("pause_reason"),
        "starting_balance_cents": start,
        "current_balance_cents": current,
        "open_positions": len(open_positions),
        "open_notional_cents": open_notional,
        "realized_pnl_cents": realized,
        "unrealized_pnl_cents": current + open_notional - start - realized,
    }


async def _list_closed_positions(
    db_path: Path, run_id: int
) -> list[dict[str, Any]]:
    import aiosqlite

    if not db_path.exists():
        return []
    try:
        async with aiosqlite.connect(str(db_path)) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM paper_positions "
                "WHERE run_id = ? AND status IN ('CLOSED', 'RESOLVED')",
                (run_id,),
            ) as cur:
                rows = await cur.fetchall()
    except aiosqlite.OperationalError:
        return []
    return [dict(r) for r in rows]
