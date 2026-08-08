"""Equity executor — applies one variant's rules to one paper run per day.

Daily reconcile:
  1. load cash + open positions + per-ticker context (price indicators + signal)
  2. run the exit ladder on open positions (skipped for buy-and-hold)
  3. select entry candidates per the variant's gates, rank, fill open slots
     under position / per-position / sector caps
  4. mark to close price, persist cash + total equity on the run

Fills at the latest observed close +/- slippage. (A forward daily loop has no
"next open" yet; close-fill is mildly optimistic and noted. Marks use close.)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date as date_cls
from pathlib import Path
from typing import Any

import aiosqlite

from polysim.equity import prices as px
from polysim.equity.universe import theme_of
from polysim.equity.variants import EquityVariant
from polysim.utils.time import iso, now_utc, parse_iso

log = logging.getLogger(__name__)


@dataclass
class TickerCtx:
    ticker: str
    close: int
    sma20: float | None
    sma50: float | None
    atr: float | None
    ret_5d: float | None
    momentum: float | None
    composite: float
    z_attn: float
    stance: float

    @property
    def uptrend(self) -> bool:
        return (self.sma50 is not None and self.sma20 is not None
                and self.close > self.sma50 and self.sma20 > self.sma50)


class EquityExecutor:
    def __init__(
        self, db_path: Path, *, run_id: int, variant: EquityVariant,
        slippage_bps: float = 5.0,
    ) -> None:
        self._db = db_path
        self.run_id = run_id
        self.v = variant
        self._slip = slippage_bps / 10_000.0

    async def tick(
        self, universe: list[str], *, day: str | None = None
    ) -> dict[str, Any]:
        day = day or now_utc().strftime("%Y-%m-%d")
        ctx = await self._build_ctx(universe)
        if not ctx:
            return {"opened": 0, "closed": 0, "note": "no price data"}
        async with aiosqlite.connect(str(self._db)) as db:
            db.row_factory = aiosqlite.Row
            cash = await self._cash(db)
            opens = await self._open_positions(db)
            closed = 0
            if not self.v.buy_and_hold:
                for p in opens:
                    c = ctx.get(p["ticker"])
                    if c is None:
                        continue
                    reason = self._exit_reason(p, c, day)
                    if reason:
                        cash += await self._close(db, p, c.close, reason, day)
                        closed += 1
                opens = await self._open_positions(db)
            held = {p["ticker"] for p in opens}
            opened = await self._enter(db, ctx, opens, held, cash, day)
            await self._mark(db, ctx, day)
        return {"opened": opened, "closed": closed, "held": len(held)}

    # ── context ──────────────────────────────────────────

    async def _build_ctx(self, universe: list[str]) -> dict[str, TickerCtx]:
        from polysim.equity.signal import latest_signals
        sigs = {s["ticker"]: s for s in await latest_signals(self._db, limit=200)}
        out: dict[str, TickerCtx] = {}
        for t in universe:
            cs = await px.closes(self._db, t)
            if not cs:
                continue
            win = await px.ohlc_window(self._db, t, limit=30)
            s = sigs.get(t, {})
            out[t] = TickerCtx(
                ticker=t, close=cs[-1],
                sma20=px.sma(cs, 20), sma50=px.sma(cs, 50),
                atr=px.atr(win, 14),
                ret_5d=px.trailing_return(cs, 5),
                momentum=px.trailing_return(cs, 63),
                composite=float(s.get("composite") or 0.0),
                z_attn=float(s.get("z_attn") or 0.0),
                stance=float(s.get("stance") or 0.0),
            )
        return out

    # ── exits ────────────────────────────────────────────

    def _exit_reason(
        self, p: dict[str, Any], c: TickerCtx, day: str
    ) -> str | None:
        entry = int(p["avg_entry_price_cents"])
        hw = max(int(p["high_water_cents"]), c.close)
        if c.close <= entry * (1 - self.v.hard_stop_pct):
            return "hard_stop"
        if c.atr is not None and c.close <= hw - self.v.atr_mult * c.atr:
            return "atr_trail"
        trail = c.sma20 if self.v.trail_sma == 20 else c.sma50
        if trail is not None and c.close < trail:
            return "trend_break"
        held_days = self._days_held(p, day)
        if held_days >= self.v.time_stop_days:
            return "time_stop"
        return None

    @staticmethod
    def _days_held(p: dict[str, Any], day: str) -> int:
        try:
            o = parse_iso(str(p["opened_at"])).date()
            d = date_cls.fromisoformat(day)
            return (d - o).days
        except Exception:
            return 0

    # ── entries ──────────────────────────────────────────

    def _entry_ok(self, c: TickerCtx) -> bool:
        v = self.v
        if v.kind == "momentum":
            return (
                c.uptrend
                and c.momentum is not None
                and (c.ret_5d is None or c.ret_5d <= v.max_5d_runup)
            )
        if v.kind == "contrarian":
            return (c.uptrend and c.z_attn <= 0.0
                    and (c.ret_5d is None or c.ret_5d <= v.max_5d_runup))
        # sentiment / attention
        if c.composite < v.min_composite:
            return False
        if c.z_attn < v.min_z_attn:
            return False
        if v.require_uptrend and not c.uptrend:
            return False
        return not (c.ret_5d is not None and c.ret_5d > v.max_5d_runup)

    def _rank(self, c: TickerCtx) -> float:
        if self.v.contrarian:
            return -c.z_attn
        if self.v.rank_by == "momentum":
            return c.momentum or -9
        if self.v.rank_by == "z_attn":
            return c.z_attn
        return c.composite

    async def _enter(
        self,
        db: aiosqlite.Connection,
        ctx: dict[str, TickerCtx],
        opens: list[dict[str, Any]],
        held: set[str],
        cash: float,
        day: str,
    ) -> int:
        v = self.v
        total_equity = cash + sum(
            p["shares"] * ctx[p["ticker"]].close
            for p in opens if p["ticker"] in ctx
        )
        if total_equity <= 0:
            return 0
        # sector exposure already on the book
        sector_val: dict[str, float] = {}
        for p in opens:
            if p["ticker"] in ctx:
                sector_val[theme_of(p["ticker"])] = sector_val.get(
                    theme_of(p["ticker"]), 0.0) + p["shares"] * ctx[p["ticker"]].close

        if v.buy_and_hold:
            if held:
                return 0
            cands = [c for c in ctx.values() if c.close > 0]
            per = total_equity / max(1, len(cands))
            opened = 0
            for c in cands:
                target = min(per, cash)
                shares = target / (c.close * (1 + self._slip))
                if shares <= 0:
                    continue
                # Spend the explicit target rather than recomputing it from
                # shares. For a one-symbol benchmark, float round-off could
                # otherwise make an all-in cost microscopically exceed cash.
                cash -= target
                await self._open(db, c, shares, day)
                opened += 1
            await self._set_cash(db, cash)
            return opened

        slots = v.max_positions - len(held)
        if slots <= 0:
            await self._set_cash(db, cash)
            return 0
        cands = sorted(
            (c for t, c in ctx.items() if t not in held and self._entry_ok(c)),
            key=self._rank, reverse=True,
        )
        base = total_equity / v.max_positions
        cap = total_equity * v.per_position_cap_pct
        sec_cap = total_equity * v.sector_cap_pct
        opened = 0
        for c in cands:
            if slots <= 0:
                break
            th = theme_of(c.ticker)
            if sector_val.get(th, 0.0) >= sec_cap:
                continue
            target = min(base, cap, sec_cap - sector_val.get(th, 0.0), cash)
            if target < total_equity * 0.02:    # too small to bother
                continue
            shares = target / (c.close * (1 + self._slip))
            cost = shares * c.close * (1 + self._slip)
            if shares <= 0 or cost > cash:
                continue
            cash -= cost
            sector_val[th] = sector_val.get(th, 0.0) + shares * c.close
            await self._open(db, c, shares, day)
            opened += 1
            slots -= 1
        await self._set_cash(db, cash)
        return opened

    # ── persistence ──────────────────────────────────────

    async def _open(
        self, db: aiosqlite.Connection, c: TickerCtx, shares: float, day: str
    ) -> None:
        await db.execute(
            """
            INSERT INTO equity_positions(
                run_id, ticker, shares, avg_entry_price_cents, high_water_cents,
                opened_at, status, source_signal
            ) VALUES (?, ?, ?, ?, ?, ?, 'OPEN', ?)
            """,
            (self.run_id, c.ticker, shares,
             round(c.close * (1 + self._slip)), c.close,
             iso(now_utc()), c.composite),
        )
        await db.commit()

    async def _close(
        self,
        db: aiosqlite.Connection,
        p: dict[str, Any],
        close_cents: int,
        reason: str,
        day: str,
    ) -> float:
        exit_px = close_cents * (1 - self._slip)
        proceeds = float(p["shares"]) * exit_px
        realized = round(p["shares"] * (exit_px - int(p["avg_entry_price_cents"])))
        await db.execute(
            "UPDATE equity_positions SET status='CLOSED', closed_at=?, "
            "exit_price_cents=?, realized_pnl_cents=?, exit_reason=? WHERE id=?",
            (iso(now_utc()), round(exit_px), realized, reason, p["id"]),
        )
        await db.commit()
        return proceeds

    async def _mark(
        self, db: aiosqlite.Connection, ctx: dict[str, TickerCtx], day: str
    ) -> None:
        opens = await self._open_positions(db)
        cash = await self._cash(db)
        mark = sum(p["shares"] * ctx[p["ticker"]].close
                   for p in opens if p["ticker"] in ctx)
        # update high-water marks
        for p in opens:
            c = ctx.get(p["ticker"])
            if c and c.close > int(p["high_water_cents"]):
                await db.execute(
                    "UPDATE equity_positions SET high_water_cents=? WHERE id=?",
                    (c.close, p["id"]),
                )
        equity = round(cash + mark)
        await db.execute(
            "UPDATE paper_runs SET current_balance_cents=? WHERE id=?",
            (equity, self.run_id),
        )
        await db.commit()

    async def _open_positions(
        self, db: aiosqlite.Connection
    ) -> list[dict[str, Any]]:
        async with db.execute(
            "SELECT * FROM equity_positions WHERE run_id=? AND status='OPEN'",
            (self.run_id,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def _cash(self, db: aiosqlite.Connection) -> float:
        async with db.execute(
            "SELECT cash_cents FROM equity_run_state WHERE run_id=?",
            (self.run_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is not None:
            return float(row["cash_cents"])
        # initialize from the run's starting balance
        async with db.execute(
            "SELECT starting_balance_cents FROM paper_runs WHERE id=?",
            (self.run_id,),
        ) as cur:
            r = await cur.fetchone()
        start = float(r["starting_balance_cents"]) if r else 0.0
        await self._set_cash(db, start)
        return start

    async def _set_cash(self, db: aiosqlite.Connection, cash: float) -> None:
        await db.execute(
            "INSERT INTO equity_run_state(run_id, cash_cents, updated_at) "
            "VALUES (?, ?, ?) ON CONFLICT(run_id) DO UPDATE SET "
            "cash_cents=excluded.cash_cents, updated_at=excluded.updated_at",
            (self.run_id, round(cash), iso(now_utc())),
        )
        await db.commit()
