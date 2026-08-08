"""Settlement-cycle simulation — empirical-priors addendum §4.2.

Polymarket reality (Kaustubh + Opus 4.6):
  * Orders match off-chain via CLOB, settle on-chain on Polygon (~2s blocks).
  * Shares bought this second are NOT sellable for ≥2 blocks (~4s min).
  * Large positions can lock trading capacity during settlement delays;
    Opus 4.6's log shows 5+ consecutive capacity-locked cycles.

Our paper executor previously treated fills as instantly fungible. This
module adds:
  1. `compute_settlement_window()` — pure function: (buy_ts, block_seconds,
     min_blocks) → (pending_until, blocks).
  2. `is_sellable()` — is a position past its pending_until?
  3. `pending_capacity_cents()` — how much capital is locked in pending
     positions right now?
  4. `sweep_settlements()` — async: move newly-settled rows from
     `settlement_events.settled_ts = NULL` to settled.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite

from polysim.utils.time import iso, now_utc, parse_iso

log = logging.getLogger(__name__)

DEFAULT_BLOCK_SECONDS = 2.0
DEFAULT_MIN_BLOCKS = 2


def compute_settlement_window(
    buy_ts: datetime,
    *,
    block_seconds: float = DEFAULT_BLOCK_SECONDS,
    min_blocks: int = DEFAULT_MIN_BLOCKS,
    extra_blocks: int = 0,
) -> tuple[datetime, int]:
    """Return `(pending_until, blocks_to_settle)` for a buy at `buy_ts`.

    `extra_blocks` lets tests / stress scenarios simulate degraded
    settlement (Opus 4.6's 5-cycle lock reproduces with extra_blocks=3).
    """
    blocks = int(min_blocks) + int(max(0, extra_blocks))
    delta = timedelta(seconds=block_seconds * blocks)
    return buy_ts + delta, blocks


def is_sellable(
    position: dict[str, Any], *, now: datetime | None = None
) -> bool:
    """True iff the position is past its settlement window (or has none)."""
    pending = position.get("pending_until_iso")
    if not pending:
        return True
    try:
        pending_dt = parse_iso(str(pending))
    except ValueError:
        return True
    return (now or now_utc()) >= pending_dt


async def pending_capacity_cents(db_path: Path, run_id: int) -> int:
    """Sum notional of positions whose settlement is still pending."""
    if not db_path.exists():
        return 0
    cutoff = iso(now_utc())
    try:
        async with aiosqlite.connect(str(db_path)) as db, db.execute(
            """
            SELECT COALESCE(SUM(size_shares * avg_entry_price_cents), 0)
            FROM paper_positions
            WHERE run_id = ? AND status = 'OPEN'
              AND pending_until_iso IS NOT NULL
              AND pending_until_iso > ?
            """,
            (run_id, cutoff),
        ) as cur:
            row = await cur.fetchone()
    except aiosqlite.OperationalError:
        return 0
    return int(row[0]) if row and row[0] is not None else 0


async def record_settlement_start(
    db_path: Path,
    *,
    position_id: int,
    buy_ts: datetime,
    pending_until: datetime,
    blocks_to_settle: int,
    capacity_locked_cents: int,
) -> int:
    """Insert a settlement_events row + stamp pending_until_iso on the position.

    Called once per paper_position at open time. Returns settlement_events.id.
    """
    async with aiosqlite.connect(str(db_path)) as db:
        await db.execute(
            "UPDATE paper_positions SET pending_until_iso = ? WHERE id = ?",
            (iso(pending_until), position_id),
        )
        cur = await db.execute(
            """
            INSERT INTO settlement_events(
                position_id, buy_ts, settled_ts, pending_until,
                blocks_to_settle, capacity_locked_cents
            ) VALUES (?, ?, NULL, ?, ?, ?)
            """,
            (
                position_id, iso(buy_ts), iso(pending_until),
                blocks_to_settle, capacity_locked_cents,
            ),
        )
        event_id = int(cur.lastrowid or 0)
        await cur.close()
        await db.commit()
    return event_id


async def sweep_settlements(db_path: Path, *, now: datetime | None = None) -> int:
    """Mark newly-settled events + clear pending_until_iso on their positions.

    Returns the count of events settled in this sweep. Safe to call on a
    cadence from the live orchestrator; no-op when nothing is due.
    """
    if not db_path.exists():
        return 0
    cutoff = iso(now or now_utc())
    try:
        async with aiosqlite.connect(str(db_path)) as db:
            cur = await db.execute(
                """
                UPDATE settlement_events
                   SET settled_ts = ?
                 WHERE settled_ts IS NULL
                   AND pending_until <= ?
                """,
                (cutoff, cutoff),
            )
            settled_count = int(cur.rowcount or 0)
            await cur.close()
            if settled_count:
                # Clear the position's pending_until_iso for any freshly
                # settled rows. Keep it simple: any OPEN position whose
                # pending_until_iso is past can be cleared.
                await db.execute(
                    """
                    UPDATE paper_positions
                       SET pending_until_iso = NULL
                     WHERE status = 'OPEN'
                       AND pending_until_iso IS NOT NULL
                       AND pending_until_iso <= ?
                    """,
                    (cutoff,),
                )
            await db.commit()
    except aiosqlite.OperationalError:
        return 0
    return settled_count


__all__ = [
    "DEFAULT_BLOCK_SECONDS",
    "DEFAULT_MIN_BLOCKS",
    "compute_settlement_window",
    "is_sellable",
    "pending_capacity_cents",
    "record_settlement_start",
    "sweep_settlements",
]
