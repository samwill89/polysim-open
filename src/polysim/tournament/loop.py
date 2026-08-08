"""TournamentAllocatorLoop — periodic rebalance of the variant pool.

Wakes up every `interval_s` seconds, scores each tournament-tagged run,
asks the allocator for verdicts, then enforces them: pauses retired
runs, resumes promoted runs. Also spawns new variants on a longer
cadence if `auto_spawn=True`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from pathlib import Path
from typing import Any

import aiosqlite

from polysim.tournament.allocator import RunScore, TournamentAllocator
from polysim.tournament.variants import (
    LIVE_VARIANT_NAMES,
    Variant,
    live_variants,
    variants_by_name,
)
from polysim.utils.time import iso, now_utc

log = logging.getLogger(__name__)

TOURNAMENT_TAG = "tournament_v1"


class TournamentAllocatorLoop:
    """Score variants → pause losers, resume winners → spawn new variants."""

    def __init__(
        self,
        db_path: Path,
        *,
        interval_s: float = 6 * 3600,  # 6h between rebalances
        spawn_interval_s: float = 7 * 24 * 3600,  # spawn new variants weekly
        allocator: TournamentAllocator | None = None,
        auto_spawn: bool = True,
    ) -> None:
        self._db = db_path
        self._interval = interval_s
        self._spawn_interval = spawn_interval_s
        self._allocator = allocator or TournamentAllocator()
        self._auto_spawn = auto_spawn
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._last_spawn_ts: float = 0.0
        self.rebalances_run = 0
        self.spawns_run = 0

    async def start(self) -> None:
        self._stop.clear()
        try:
            await self.seed_pool()
        except Exception as exc:
            log.warning("tournament: initial pool reconciliation failed: %s", exc)
        self._task = asyncio.create_task(self._run(), name="tournament-allocator")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self._tick_once()
            except Exception as exc:
                log.warning("tournament rebalance failed: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
                return
            except TimeoutError:
                continue

    async def _tick_once(self) -> None:
        scores = await self._score_active_runs()
        if not scores:
            return
        decisions = self._allocator.rebalance(scores)
        for d in decisions:
            if d.verdict == "retire":
                await self._pause_run(
                    d.run_id,
                    reason=f"tournament_allocator: {d.reason}",
                )
                log.info(
                    "tournament: retire #%d (%s) — %s",
                    d.run_id,
                    d.variant_name,
                    d.reason,
                )
            elif d.verdict == "promote":
                await self._resume_run(d.run_id)
                log.info(
                    "tournament: promote #%d (%s) — %s",
                    d.run_id,
                    d.variant_name,
                    d.reason,
                )
        self.rebalances_run += 1
        if self._auto_spawn:
            now = now_utc().timestamp()
            if (now - self._last_spawn_ts) >= self._spawn_interval:
                try:
                    await self.seed_pool()
                    self._last_spawn_ts = now
                except Exception as exc:
                    log.warning("tournament: spawn cycle failed: %s", exc)

    async def seed_pool(self) -> int:
        """Pause retired lanes and ensure every live variant has an open run.
        Returns the number of new runs created."""
        await self._pause_inactive_runs()
        existing = await self._existing_variant_names()
        created = 0
        for v in live_variants():
            if v.name in existing:
                continue
            try:
                await self._open_variant_run(v)
                created += 1
            except Exception as exc:
                log.warning(
                    "tournament: failed to open variant %s: %s",
                    v.name,
                    exc,
                )
        if created:
            self.spawns_run += created
            log.info("tournament: spawned %d new variant runs", created)
        return created

    # ── helpers ──────────────────────────────────────────

    async def _existing_variant_names(self) -> set[str]:
        if not self._db.exists():
            return set()
        async with (
            aiosqlite.connect(str(self._db)) as conn,
            conn.execute(
                "SELECT profile_snapshot_json FROM paper_runs WHERE tag = ? AND ended_at IS NULL",
                (TOURNAMENT_TAG,),
            ) as cur,
        ):
            rows = await cur.fetchall()
        names: set[str] = set()
        for r in rows:
            if r and r[0]:
                try:
                    snap = json.loads(r[0])
                    if isinstance(snap, dict) and snap.get("name"):
                        names.add(str(snap["name"]))
                except (ValueError, TypeError):
                    continue
        return names

    async def _pause_inactive_runs(self) -> int:
        if not self._db.exists():
            return 0
        live = set(LIVE_VARIANT_NAMES)
        paused = 0
        async with aiosqlite.connect(str(self._db)) as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute(
                "SELECT id, profile_snapshot_json, pause_reason FROM paper_runs "
                "WHERE tag = ? AND ended_at IS NULL",
                (TOURNAMENT_TAG,),
            ) as cur:
                rows = list(await cur.fetchall())
            for row in rows:
                name = self._variant_name_from(row["profile_snapshot_json"])
                if name in live:
                    continue
                retirement_reason = "strategy_set_v3: superseded by verified-edge controls"
                if str(row["pause_reason"] or "") == retirement_reason:
                    continue
                await conn.execute(
                    "UPDATE paper_runs SET paused_at = COALESCE(paused_at, ?), "
                    "pause_reason = ? WHERE id = ?",
                    (
                        iso(now_utc()),
                        retirement_reason,
                        int(row["id"]),
                    ),
                )
                paused += 1
            await conn.commit()
        if paused:
            log.info("tournament: paused %d retired variant runs", paused)
        return paused

    async def _open_variant_run(self, variant: Variant) -> int:
        from polysim.profiles import load_profile

        base = load_profile(variant.base_profile)
        snap = variant.merged_profile_dict(base.model_dump())
        async with aiosqlite.connect(str(self._db)) as conn:
            cur = await conn.execute(
                """
                INSERT INTO paper_runs(
                    name, started_at, config_json,
                    starting_balance_cents, current_balance_cents, notes,
                    profile_name, profile_snapshot_json, tag
                ) VALUES (?, ?, '{}', ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"tournament-{variant.name}",
                    iso(now_utc()),
                    variant.starting_balance_cents,
                    variant.starting_balance_cents,
                    variant.description,
                    variant.base_profile,
                    json.dumps(snap, default=str),
                    TOURNAMENT_TAG,
                ),
            )
            new_id = int(cur.lastrowid or 0)
            await cur.close()
            await conn.commit()
        return new_id

    async def _score_active_runs(self) -> list[RunScore]:
        if not self._db.exists():
            return []
        async with aiosqlite.connect(str(self._db)) as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute(
                "SELECT id, profile_snapshot_json, starting_balance_cents, "
                "current_balance_cents, paused_at, pause_reason "
                "FROM paper_runs WHERE tag = ? AND ended_at IS NULL",
                (TOURNAMENT_TAG,),
            ) as cur:
                runs = list(await cur.fetchall())
            # One bid lookup per (market, outcome) across all runs per tick.
            bid_cache: dict[tuple[str, str], int | None] = {}
            scores: list[RunScore] = []
            for r in runs:
                if str(r["pause_reason"] or "").startswith("strategy_set_v3:"):
                    continue
                async with conn.execute(
                    "SELECT COUNT(*), "
                    "COALESCE(SUM(realized_pnl_cents), 0), "
                    "SUM(CASE WHEN status = 'RESOLVED' THEN 1 ELSE 0 END) "
                    "FROM paper_positions WHERE run_id = ?",
                    (int(r["id"]),),
                ) as pcur:
                    prow = await pcur.fetchone()
                n_pos = int(prow[0]) if prow else 0
                realized = int(prow[1]) if prow else 0
                n_resolved = int(prow[2]) if prow and prow[2] is not None else 0
                # Bid-price MTM on open positions (§4.1 — bid, never mid).
                # A position with no book snapshot marks at entry
                # (unrealized contribution 0 — the old conservative
                # behavior, now per-position instead of blanket).
                unrealized = await self._unrealized_at_bid(
                    int(r["id"]),
                    bid_cache,
                )
                variant_name = self._variant_name_from(
                    r["profile_snapshot_json"],
                )
                start = int(r["starting_balance_cents"])
                cur_bal = int(r["current_balance_cents"])
                # Drawdown vs equity (cash + marked open positions), not
                # cash alone — deploying capital is not a loss.
                open_cost = await self._open_cost_cents(conn, int(r["id"]))
                equity = cur_bal + open_cost + unrealized
                max_dd_pct = 0.0
                if start > 0 and equity < start:
                    max_dd_pct = (start - equity) / start
                scores.append(
                    RunScore(
                        run_id=int(r["id"]),
                        variant_name=variant_name,
                        starting_balance_cents=start,
                        current_balance_cents=cur_bal,
                        realized_pnl_cents=realized,
                        unrealized_pnl_cents=unrealized,
                        n_positions=n_pos,
                        n_resolved_positions=n_resolved,
                        max_drawdown_pct=max_dd_pct,
                        paused=bool(r["paused_at"]),
                    )
                )
        return scores

    @staticmethod
    async def _open_cost_cents(conn: aiosqlite.Connection, run_id: int) -> int:
        async with conn.execute(
            "SELECT COALESCE(SUM(size_shares * avg_entry_price_cents), 0) "
            "FROM paper_positions WHERE run_id = ? AND status = 'OPEN'",
            (run_id,),
        ) as cur:
            row = await cur.fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    async def _unrealized_at_bid(
        self,
        run_id: int,
        bid_cache: dict[tuple[str, str], int | None],
    ) -> int:
        """Sum of (bid - entry) * size over OPEN positions, marking only
        positions that have a current orderbook snapshot (bid). Positions
        without a book contribute 0 — same as marking at entry."""
        from polysim.portfolio.valuation import best_bid_ask_from_snapshot

        async with aiosqlite.connect(str(self._db)) as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute(
                "SELECT market_id, outcome, size_shares, avg_entry_price_cents "
                "FROM paper_positions WHERE run_id = ? AND status = 'OPEN'",
                (run_id,),
            ) as cur:
                rows = list(await cur.fetchall())
        total = 0
        for row in rows:
            key = (str(row["market_id"]), str(row["outcome"]))
            if key not in bid_cache:
                pair = await best_bid_ask_from_snapshot(
                    self._db,
                    market_id=key[0],
                    outcome=key[1],
                )
                bid_cache[key] = pair[0] if pair is not None else None
            bid = bid_cache[key]
            if bid is None:
                continue
            size = int(row["size_shares"])
            entry = int(row["avg_entry_price_cents"])
            total += size * (bid - entry)
        return total

    @staticmethod
    def _variant_name_from(snapshot_json: Any) -> str:
        if not snapshot_json:
            return "unknown"
        try:
            snap = json.loads(str(snapshot_json))
        except (ValueError, TypeError):
            return "unknown"
        if isinstance(snap, dict):
            return str(snap.get("name") or "unknown")
        return "unknown"

    async def _pause_run(self, run_id: int, *, reason: str) -> None:
        async with aiosqlite.connect(str(self._db)) as conn:
            await conn.execute(
                "UPDATE paper_runs SET paused_at = ?, pause_reason = ? "
                "WHERE id = ? AND paused_at IS NULL",
                (iso(now_utc()), reason, run_id),
            )
            await conn.commit()

    async def _resume_run(self, run_id: int) -> None:
        async with aiosqlite.connect(str(self._db)) as conn:
            await conn.execute(
                "UPDATE paper_runs SET paused_at = NULL, pause_reason = NULL WHERE id = ?",
                (run_id,),
            )
            await conn.commit()


# Expose the variant name lookup for callers that want it.
_ = variants_by_name
