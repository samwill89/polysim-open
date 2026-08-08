"""Null + favorite-mid baseline runners — build plan §5.4 / spec §10.

Both baselines consume the primary run's positions (one per flag that was
actually acted on) and produce a matched, independently-funded paper_run
that can be diffed against the primary via significance.py.

  * Null: same markets, random direction (seeded), same size/entry price
    as the primary — tests whether primary's edge is real or an artifact
    of being long the markets the operator's detectors picked.
  * Favorite-mid: always BUY YES at the primary's intended price — tests
    whether a naive "always back the favourite" strategy in the same
    markets does as well.

The baselines do NOT re-run the scorer or the investigator; they are
cheap post-hoc counterfactuals.
"""

from __future__ import annotations

import logging
import random
from pathlib import Path
from typing import Any

from polysim.db import dao

log = logging.getLogger(__name__)


async def _load_primary_closed(
    db_path: Path, primary_run_id: int
) -> list[dict[str, Any]]:
    import aiosqlite

    async with aiosqlite.connect(str(db_path)) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM paper_positions "
            "WHERE run_id = ? AND status IN ('CLOSED', 'RESOLVED') "
            "ORDER BY opened_at",
            (primary_run_id,),
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def run_null_baseline(
    db_path: Path,
    *,
    primary_run_id: int,
    starting_balance_cents: int,
    seed: int = 0,
    name_suffix: str = "null",
) -> int:
    """Create a null baseline run. Returns new run id."""
    primary = await _load_primary_closed(db_path, primary_run_id)
    if not primary:
        raise ValueError(
            f"primary run #{primary_run_id} has no closed positions; cannot build baseline"
        )

    new_id = await dao.create_paper_run(
        db_path,
        name=f"{primary_run_id}-{name_suffix}",
        starting_balance_cents=starting_balance_cents,
        config_snapshot={
            "baseline": "null",
            "primary_run_id": primary_run_id,
            "seed": seed,
        },
        notes=f"null baseline of run #{primary_run_id} (seed={seed})",
    )
    rng = random.Random(seed)

    for pos in primary:
        market_id = str(pos["market_id"])
        market = await dao.get_market(db_path, market_id)
        if market is None:
            continue
        size = int(pos.get("size_shares") or 0)
        entry = int(pos.get("avg_entry_price_cents") or 0)
        if size <= 0 or entry <= 0:
            continue
        # Random direction — independent of the insider's side.
        outcome = rng.choice(["YES", "NO"])

        # Open a paper_position + fill at the primary's entry price.
        pos_id = await dao.write_paper_position(
            db_path,
            run_id=new_id,
            market_id=market_id,
            outcome=outcome,
            size_shares=size,
            avg_entry_price_cents=entry,
            source_flag_id=int(pos.get("source_flag_id") or 0) or None,
            source_wallet=None,
        )
        await dao.write_paper_fill(
            db_path,
            run_id=new_id,
            position_id=pos_id,
            side="BUY",
            size_shares=size,
            fill_price_cents=entry,
            intended_price_cents=entry,
            slippage_cents=0,
            latency_ms=0,
            fee_cents=0,
        )
        await dao.adjust_run_balance(db_path, new_id, -(size * entry))

        # Close on resolved outcome.
        if market.resolved_outcome is not None:
            invalid = market.resolved_outcome == "INVALID"
            payout_per_share = (
                0 if invalid
                else (100 if outcome == market.resolved_outcome else 0)
            )
            if invalid:
                realized = 0
                # Spec §9 — invalid refunds entry cost.
                await dao.adjust_run_balance(db_path, new_id, size * entry)
            else:
                realized = size * (payout_per_share - entry)
                await dao.adjust_run_balance(
                    db_path, new_id, size * payout_per_share
                )
            await dao.close_position(
                db_path, pos_id,
                realized_pnl_cents=realized, status="RESOLVED",
            )

    return new_id


async def run_favorite_baseline(
    db_path: Path,
    *,
    primary_run_id: int,
    starting_balance_cents: int,
    name_suffix: str = "favorite",
) -> int:
    """Create a favorite-mid baseline: always BUY YES at primary's entry.

    Uses the SAME size, price, and market as the primary position — only
    direction is fixed to YES. A no-edge strategy in this setting.
    """
    primary = await _load_primary_closed(db_path, primary_run_id)
    if not primary:
        raise ValueError(
            f"primary run #{primary_run_id} has no closed positions; cannot build baseline"
        )

    new_id = await dao.create_paper_run(
        db_path,
        name=f"{primary_run_id}-{name_suffix}",
        starting_balance_cents=starting_balance_cents,
        config_snapshot={
            "baseline": "favorite_mid",
            "primary_run_id": primary_run_id,
        },
        notes=f"favorite-mid baseline of run #{primary_run_id}",
    )

    for pos in primary:
        market_id = str(pos["market_id"])
        market = await dao.get_market(db_path, market_id)
        if market is None:
            continue
        size = int(pos.get("size_shares") or 0)
        entry = int(pos.get("avg_entry_price_cents") or 0)
        if size <= 0 or entry <= 0:
            continue

        outcome = "YES"
        pos_id = await dao.write_paper_position(
            db_path,
            run_id=new_id,
            market_id=market_id,
            outcome=outcome,
            size_shares=size,
            avg_entry_price_cents=entry,
            source_flag_id=int(pos.get("source_flag_id") or 0) or None,
            source_wallet=None,
        )
        await dao.write_paper_fill(
            db_path,
            run_id=new_id,
            position_id=pos_id,
            side="BUY",
            size_shares=size,
            fill_price_cents=entry,
            intended_price_cents=entry,
            slippage_cents=0,
            latency_ms=0,
            fee_cents=0,
        )
        await dao.adjust_run_balance(db_path, new_id, -(size * entry))

        if market.resolved_outcome is not None:
            invalid = market.resolved_outcome == "INVALID"
            if invalid:
                realized = 0
                await dao.adjust_run_balance(db_path, new_id, size * entry)
            else:
                payout = 100 if outcome == market.resolved_outcome else 0
                realized = size * (payout - entry)
                await dao.adjust_run_balance(db_path, new_id, size * payout)
            await dao.close_position(
                db_path, pos_id,
                realized_pnl_cents=realized, status="RESOLVED",
            )

    return new_id
