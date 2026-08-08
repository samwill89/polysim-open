"""Paper executor — consumes approved flags, simulates copy fills, closes on resolution.

Spec §7.6 + build plan §4.2 / §4.4 / §4.9 / §4.10.

on_flag(flag):
  1. kill-switch check (§14 #7)
  2. size the copy trade:
       min(fixed_copy_cents,
           max_pct_per_position * balance,
           max_pct_per_market * market.daily_volume_usd_cents)
     scaled by G9 composite factor.
  3. portfolio.can_open() gate
  4. fill_model.simulate()
  5. write paper_position + paper_fill; deduct cost from balance

on_resolution(market):
  * list OPEN positions for this market
  * close each at $1 / $0 / $0 (INVALID)
  * credit payout back to balance

on_source_wallet_sell(trade): G10 mirror_exits
  * for each OPEN position sourced from this wallet + same market,
    reduce proportionally.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from polysim.config import BankrollConfig
from polysim.db import dao
from polysim.paper import kill_switches
from polysim.paper.fill_model import (
    BookLevel,
    FillModel,
    FillRequest,
    OrderbookSnapshot,
)
from polysim.paper.portfolio import Portfolio
from polysim.trading.costs import taker_fee_cents
from polysim.utils.time import iso, parse_iso

log = logging.getLogger(__name__)


class PaperExecutor:
    def __init__(
        self,
        db_path: Path,
        *,
        run_id: int,
        bankroll: BankrollConfig,
        fill_model: FillModel,
    ) -> None:
        self._db = db_path
        self._run_id = run_id
        self._cfg = bankroll
        self._fill_model = fill_model
        self._portfolio = Portfolio(db_path, run_id=run_id, bankroll=bankroll)

    @property
    def portfolio(self) -> Portfolio:
        return self._portfolio

    @property
    def run_id(self) -> int:
        return self._run_id

    # ── flag handling ────────────────────────────────────

    async def on_flag(self, flag_id: int) -> int | None:
        """Process one approved flag. Returns new position id, or None if skipped."""
        # Kill switches first — cheap, short-circuits the rest.
        tripped = await kill_switches.check_and_pause(
            self._db,
            self._run_id,
            max_drawdown_pct=self._cfg.kill_drawdown_pct,
            max_flags_per_hour=self._cfg.kill_flags_per_hour,
        )
        if tripped is not None:
            log.warning(
                "run #%d paused by %s switch: %s",
                self._run_id,
                tripped.code,
                tripped.detail,
            )
            return None

        flag = await dao.get_flag(self._db, flag_id)
        if flag is None:
            return None

        # Spec §7.5 / §14 #6 — act only when investigator INFORMED (if present).
        verdict = flag.get("investigator_verdict")
        acted_on = bool(flag.get("acted_on"))
        if verdict is not None and verdict != "INFORMED":
            return None
        if verdict == "INFORMED" and not acted_on:
            # Investigator labelled INFORMED but confidence too low -> skip.
            return None

        market_id = str(flag.get("market_id"))
        market = await dao.get_market(self._db, market_id)
        if market is None or market.resolved_outcome is not None:
            # Don't open positions into already-resolved markets.
            return None

        source_wallet = str(flag.get("wallet_address") or "").lower()

        # Determine copy size.
        balance = await self._portfolio.current_balance_cents()
        if balance <= 0:
            log.info("run #%d has non-positive balance; skipping flag %s", self._run_id, flag_id)
            return None

        composite = float(flag.get("composite_score") or 0.0)
        target_cents = self._size_copy_cents(balance=balance, composite=composite, market=market)
        if target_cents <= 0:
            return None

        # Decide side + outcome from the insider's triggering trade.
        trade_row = await _fetch_trade(self._db, flag.get("trade_id"))
        if trade_row is None:
            # Fall back to the composite's per-trade metadata; skip if none.
            log.info("flag %s has no resolvable triggering trade — skipping", flag_id)
            return None

        side = str(trade_row["side"])
        outcome = str(trade_row["outcome"])
        insider_price_cents = int(trade_row["price_cents"])
        insider_ts = parse_iso(str(trade_row["timestamp"]))

        # Translate target cents -> share count at insider's price.
        # (Pre-slippage estimate; fill model refines.)
        intended_shares = max(1, target_cents // max(1, insider_price_cents))

        # Portfolio caps.
        check = await self._portfolio.can_open(
            market_id=market_id,
            source_wallet=source_wallet,
            intended_notional_cents=intended_shares * insider_price_cents,
        )
        if not check.allowed:
            log.info("portfolio refused flag #%s: %s", flag_id, check.reason)
            return None

        # Orderbook snapshot.
        book = await _get_orderbook_snapshot(
            self._db,
            market_id=market_id,
            outcome=outcome,
            at=insider_ts,
        )

        request = FillRequest(
            market_id=market_id,
            side=side,  # type: ignore[arg-type]
            outcome=outcome,  # type: ignore[arg-type]
            intended_size_shares=intended_shares,
            intended_price_cents=insider_price_cents,
            insider_trade_timestamp=insider_ts,
        )
        result = await self._fill_model.simulate(request, book)

        if result.filled_size_shares <= 0:
            log.info("flag #%s filled 0 shares — skipping", flag_id)
            return None

        protocol_fee = taker_fee_cents(
            shares=result.filled_size_shares,
            price_cents=result.fill_price_cents,
            category=market.category,
            metadata=market.metadata,
        )
        if protocol_fee:
            result = replace(
                result,
                fee_cents=result.fee_cents + protocol_fee,
            )

        # Persist the new position + fill, deduct cost from balance.
        position_id = await dao.write_paper_position(
            self._db,
            run_id=self._run_id,
            market_id=market_id,
            outcome=outcome,
            size_shares=result.filled_size_shares,
            avg_entry_price_cents=result.fill_price_cents,
            source_flag_id=int(flag.get("id") or flag_id),
            source_wallet=source_wallet,
        )
        # Settlement: §4.2 — every fill enters a 2-block pending window.
        from polysim.portfolio.settlement import (
            compute_settlement_window,
            record_settlement_start,
        )
        from polysim.utils.time import now_utc

        buy_ts = now_utc()
        pending_until, blocks = compute_settlement_window(buy_ts)
        await record_settlement_start(
            self._db,
            position_id=position_id,
            buy_ts=buy_ts,
            pending_until=pending_until,
            blocks_to_settle=blocks,
            capacity_locked_cents=result.filled_size_shares * result.fill_price_cents,
        )
        await dao.write_paper_fill(
            self._db,
            run_id=self._run_id,
            position_id=position_id,
            side=side,
            size_shares=result.filled_size_shares,
            fill_price_cents=result.fill_price_cents,
            intended_price_cents=insider_price_cents,
            slippage_cents=result.slippage_cents,
            latency_ms=result.latency_ms,
            fee_cents=result.fee_cents,
        )
        # Deduct cost + fee from bankroll.
        cost = result.filled_size_shares * result.fill_price_cents + result.fee_cents
        await self._portfolio.apply_cost(-cost)

        log.info(
            "opened paper position #%d  flag=%s  size=%d @ %d¢  slip=%d  lat=%dms",
            position_id,
            flag_id,
            result.filled_size_shares,
            result.fill_price_cents,
            result.slippage_cents,
            result.latency_ms,
        )
        return position_id

    # ── resolution ───────────────────────────────────────

    async def on_resolution(self, market_id: str) -> list[int]:
        """Close all OPEN positions for `market_id`. Returns closed position ids."""
        market = await dao.get_market(self._db, market_id)
        if market is None or market.resolved_outcome is None:
            return []

        resolved_outcome = market.resolved_outcome
        invalid = resolved_outcome == "INVALID"
        closed: list[int] = []

        positions = await dao.list_open_positions_for_market(self._db, market_id)
        for pos in positions:
            if int(pos.get("run_id") or 0) != self._run_id:
                continue
            position_id = int(pos["id"])
            pos_outcome = str(pos.get("outcome") or "")
            payout = 0 if invalid else (100 if pos_outcome == resolved_outcome else 0)
            await self._portfolio.close_position_at_payout(
                position_id,
                payout_per_share_cents=payout,
                invalid=invalid,
            )
            closed.append(position_id)
            log.info(
                "closed position #%d at %d¢/share (market %s=%s)",
                position_id,
                payout,
                market_id,
                resolved_outcome,
            )
        return closed

    # ── G10 mirror exits ─────────────────────────────────

    async def on_source_wallet_sell(
        self,
        *,
        source_wallet: str,
        market_id: str,
        sold_shares: int,
        total_source_position_before: int,
    ) -> int:
        """Proportionally reduce our OPEN position sourced from this wallet.

        Only runs when bankroll.mirror_exits is True. Returns shares reduced.
        """
        if not self._cfg.mirror_exits:
            return 0
        if total_source_position_before <= 0 or sold_shares <= 0:
            return 0

        pos = await dao.get_open_position_for_market(self._db, self._run_id, market_id)
        if pos is None:
            return 0
        if str(pos.get("source_wallet") or "").lower() != source_wallet.lower():
            return 0

        fraction = min(1.0, sold_shares / total_source_position_before)
        our_size = int(pos.get("size_shares") or 0)
        cut = max(1, int(our_size * fraction))
        cut = min(cut, our_size)

        # Sell `cut` shares at the last-known fill price (a simplification — a
        # richer impl would re-query the book; Phase 4 accepts this).
        avg_entry = int(pos.get("avg_entry_price_cents") or 0)
        market = await dao.get_market(self._db, market_id)
        fee_cents = taker_fee_cents(
            shares=cut,
            price_cents=avg_entry,
            category=(market.category if market else None),
            metadata=(market.metadata if market else None),
        )
        proceeds = cut * avg_entry - fee_cents
        await self._portfolio.apply_cost(proceeds)

        new_size = our_size - cut
        if new_size <= 0:
            await dao.close_position(
                self._db,
                int(pos["id"]),
                realized_pnl_cents=-fee_cents,
                status="CLOSED",
            )
        else:
            await dao.update_position_size(
                self._db,
                int(pos["id"]),
                new_size_shares=new_size,
                new_avg_entry_price_cents=avg_entry,
            )
        await dao.write_paper_fill(
            self._db,
            run_id=self._run_id,
            position_id=int(pos["id"]),
            side="SELL",
            size_shares=cut,
            fill_price_cents=avg_entry,
            intended_price_cents=avg_entry,
            slippage_cents=0,
            latency_ms=0,
            fee_cents=fee_cents,
        )
        return cut

    # ── sizing (G9) ──────────────────────────────────────

    def _size_copy_cents(
        self,
        *,
        balance: int,
        composite: float,
        market: Any,
    ) -> int:
        """Spec §7.6 copy-sizing + G9 composite scaling.

        size = min(fixed, max_pct_per_position*balance, max_pct_per_market*vol)
               * clamp(composite/divisor, floor, ceiling)
        """
        base = self._cfg.fixed_copy_cents
        pos_cap = int(balance * self._cfg.max_pct_per_position)
        vol = int(market.daily_volume_usd_cents or 0) if market is not None else 0
        vol_cap = int(vol * self._cfg.max_pct_per_market) if vol > 0 else base

        candidates = [c for c in (base, pos_cap, vol_cap) if c > 0]
        if not candidates:
            return 0
        raw = min(candidates)

        divisor = max(1e-9, self._cfg.copy_size_divisor)
        scale = max(
            self._cfg.copy_size_floor,
            min(self._cfg.copy_size_ceiling, composite / divisor),
        )
        return max(0, int(raw * scale))


# ── helpers ──────────────────────────────────────────────


async def _fetch_trade(db_path: Path, trade_id: Any) -> dict[str, Any] | None:
    if trade_id is None:
        return None
    import aiosqlite

    async with aiosqlite.connect(str(db_path)) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM trades WHERE id = ?", (str(trade_id),)) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def _get_orderbook_snapshot(
    db_path: Path,
    *,
    market_id: str,
    outcome: str,
    at: datetime,
) -> OrderbookSnapshot | None:
    row = await dao.get_nearest_orderbook_snapshot(
        db_path,
        market_id=market_id,
        outcome=outcome,
        at_iso=iso(at),
        max_age_seconds=600,  # 10min tolerance
    )
    if row is None:
        return None
    import json

    asks_raw = json.loads(row["asks_json"])
    bids_raw = json.loads(row["bids_json"])
    return OrderbookSnapshot(
        market_id=market_id,
        outcome=outcome,  # type: ignore[arg-type]
        asks=[
            BookLevel(price_cents=int(a["price_cents"]), size_shares=int(a["size_shares"]))
            for a in asks_raw
        ],
        bids=[
            BookLevel(price_cents=int(b["price_cents"]), size_shares=int(b["size_shares"]))
            for b in bids_raw
        ],
        timestamp=parse_iso(str(row["timestamp"])),
    )
