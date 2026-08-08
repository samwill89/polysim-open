"""Profile-aware paper executor — addendum §5.

Wraps `PaperExecutor` with:
  * Signal filters      (min_composite_score, allowed_detectors)
  * Market filters      (min/max odds, daily-volume bounds)
  * Portfolio sizing    (fixed / percentage / kelly_fractional)
  * Risk-management     (drawdown_limit_pct, daily_loss_limit_pct)

The base executor keeps running the existing kill switches (flag-rate) and
the copy-size divisor logic; this layer replaces the per-position sizing
math and adds profile-scoped pre-gates. Sizing respects both the base
portfolio caps AND the profile's own `max_pct_per_position` bound.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from polysim.config import BankrollConfig, RiskProfile
from polysim.db import dao
from polysim.paper import kill_switches
from polysim.paper.executor import (
    PaperExecutor,
    _fetch_trade,
    _get_orderbook_snapshot,
)
from polysim.paper.fill_model import FillModel, FillRequest
from polysim.trading.costs import taker_fee_cents
from polysim.utils.time import iso, now_utc, parse_iso

if TYPE_CHECKING:
    from polysim.risk_intelligence.service import RiskIntelligenceService

log = logging.getLogger(__name__)


class ProfilePaperExecutor:
    """Addendum-§5 executor. Composes over the single-profile PaperExecutor."""

    def __init__(
        self,
        db_path: Path,
        *,
        run_id: int,
        profile: RiskProfile,
        bankroll: BankrollConfig,
        fill_model: FillModel,
        risk_intelligence: RiskIntelligenceService | None = None,
    ) -> None:
        self._db = db_path
        self._run_id = run_id
        self._profile = profile
        self._bankroll = bankroll
        self._risk_intelligence = risk_intelligence
        self._inner = PaperExecutor(
            db_path,
            run_id=run_id,
            bankroll=bankroll,
            fill_model=fill_model,
        )

    @property
    def run_id(self) -> int:
        return self._run_id

    @property
    def profile(self) -> RiskProfile:
        return self._profile

    @property
    def inner(self) -> PaperExecutor:
        return self._inner

    # ── main entry point ─────────────────────────────────

    async def consider_flag(self, flag_id: int) -> int | None:
        """Addendum §5 `consider_flag` — profile-gated copy open."""
        # 1. Base kill switches (flag_rate) + drawdown + daily-loss.
        if await self._pre_gate_tripped():
            return None

        flag = await dao.get_flag(self._db, flag_id)
        if flag is None:
            return None

        # Investigator gate — unchanged from base executor.
        verdict = flag.get("investigator_verdict")
        acted_on = bool(flag.get("acted_on"))
        if verdict is not None and verdict != "INFORMED":
            return None
        if verdict == "INFORMED" and not acted_on:
            return None

        # 2. Signal filters.
        composite = float(flag.get("composite_score") or 0.0)
        if composite < self._profile.min_composite_score:
            return None
        if not self._contributing_ok(flag):
            return None

        # 3. Market + fetch.
        market_id = str(flag.get("market_id"))
        market = await dao.get_market(self._db, market_id)
        if market is None or market.resolved_outcome is not None:
            return None

        # Triggering trade — needed for odds, side, and fill-model.
        trade_row = await _fetch_trade(self._db, flag.get("trade_id"))
        if trade_row is None:
            return None
        side = str(trade_row["side"])
        outcome = str(trade_row["outcome"])
        insider_price_cents = int(trade_row["price_cents"])
        insider_ts = parse_iso(str(trade_row["timestamp"]))

        # Current implied odds = price / 100.
        current_odds = insider_price_cents / 100.0

        # 4. Market filters.
        if not self._market_odds_ok(current_odds):
            return None
        if (
            self._profile.max_entry_price_cents is not None
            and insider_price_cents > self._profile.max_entry_price_cents
        ):
            return None
        if (
            self._profile.min_source_trade_notional_cents is not None
            and int(trade_row["size_shares"]) * insider_price_cents
            < self._profile.min_source_trade_notional_cents
        ):
            return None
        if self._profile.allowed_market_categories:
            allowed_categories = {
                category.strip().lower()
                for category in self._profile.allowed_market_categories
                if category.strip()
            }
            market_category = str(market.category or "unknown").lower()
            if market_category not in allowed_categories:
                return None
        if not self._market_volume_ok(market.daily_volume_usd_cents):
            return None

        # 4a. Catalyst-sensitive evidence snapshot. Ordinary markets avoid
        # network work entirely. A required gate fails closed when its shared
        # service is unavailable; monitor mode only records what it can.
        risk_assessment = None
        if self._profile.risk_intelligence_policy != "off":
            from polysim.risk_intelligence.scoring import is_catalyst_sensitive

            catalyst_sensitive = is_catalyst_sensitive(market.question)
            if self._risk_intelligence is None:
                if (
                    catalyst_sensitive
                    and self._profile.risk_intelligence_policy == "require_verified_edge"
                ):
                    log.warning(
                        "risk gate: run #%d skipped sensitive market %s - "
                        "risk-intelligence service unavailable",
                        self._run_id,
                        market_id,
                    )
                    return None
            else:
                try:
                    risk_assessment = await self._risk_intelligence.assessment_for(market)
                except Exception as exc:
                    log.warning(
                        "risk gate: evidence assessment failed for %s: %s",
                        market_id,
                        exc,
                    )
                    if (
                        catalyst_sensitive
                        and self._profile.risk_intelligence_policy == "require_verified_edge"
                    ):
                        return None

        # 4b. External conversation signal (signals package) - off for all
        #     profiles unless signal_policy is set. Direction still comes
        #     from the cohort wallet; the signal only scales conviction
        #     (and, in gate_and_size, vetoes confidently-dead conversations).
        #     Missing/stale/failed signal degrades to neutral, never blocks.
        signal_composite: float | None = None
        signal_multiplier: float | None = None
        if self._profile.signal_policy != "off":
            signal_should_block, signal_mult, signal_score = await self._signal_adjustment(
                market_id
            )
            if signal_should_block:
                log.info(
                    "signal gate: run #%d profile=%s skipped flag #%s - "
                    "dead conversation (composite=%.2f)",
                    self._run_id,
                    self._profile.name,
                    flag_id,
                    signal_score or 0.0,
                )
                return None
            if signal_mult is not None:
                signal_multiplier = signal_mult
            signal_composite = signal_score

        # 5. Size the position per profile.
        balance = await self._inner.portfolio.current_balance_cents()
        if balance <= 0:
            return None
        target_cents = self._size_position(balance=balance, flag=flag, current_odds=current_odds)
        if signal_multiplier is not None and signal_multiplier != 1.0:
            target_cents = int(target_cents * signal_multiplier)
        if target_cents <= 0:
            return None

        # Respect profile hard cap + base portfolio caps.
        intended_shares = max(1, target_cents // max(1, insider_price_cents))
        intended_notional = intended_shares * insider_price_cents

        source_wallet = str(flag.get("wallet_address") or "").lower()

        # 5b. Profile-specific portfolio caps (may be stricter than bankroll).
        if not await self._profile_portfolio_ok(
            balance=balance,
            market_id=market_id,
            source_wallet=source_wallet,
            intended_notional=intended_notional,
        ):
            return None

        # Delegate to base portfolio for max-open-positions / one-per-market.
        check = await self._inner.portfolio.can_open(
            market_id=market_id,
            source_wallet=source_wallet,
            intended_notional_cents=intended_notional,
        )
        if not check.allowed:
            log.info(
                "run #%d profile=%s refused flag #%s: %s",
                self._run_id,
                self._profile.name,
                flag_id,
                check.reason,
            )
            return None

        # 6. Depth cap (§4.4): cap intended size to fraction of top-3 depth.
        from polysim.portfolio.depth import (
            DEFAULT_DEGEN_DEPTH_PCT,
            DEFAULT_SYSTEMATIC_DEPTH_PCT,
            cap_size_to_depth,
            latest_book_for,
        )

        depth_cap_pct = (
            DEFAULT_DEGEN_DEPTH_PCT
            if self._profile.name == "degen"
            else DEFAULT_SYSTEMATIC_DEPTH_PCT
        )
        book_pair = await latest_book_for(
            self._db,
            market_id=market_id,
            outcome=outcome,
        )
        if book_pair is not None:
            bids, asks = book_pair
            capped = cap_size_to_depth(
                intended_shares,
                side=side,
                bids=bids,
                asks=asks,
                cap_pct=depth_cap_pct,
            )
            if capped <= 0:
                log.info(
                    "depth check: no usable depth for %s/%s — skipping flag %s",
                    market_id,
                    outcome,
                    flag_id,
                )
                return None
            if capped < intended_shares:
                log.info(
                    "depth cap: profile=%s reduced %d → %d shares (cap_pct=%.2f)",
                    self._profile.name,
                    intended_shares,
                    capped,
                    depth_cap_pct,
                )
                intended_shares = capped

        # 7. Simulate fill.
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
        result = await self._inner._fill_model.simulate(request, book)
        if result.filled_size_shares <= 0:
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

        # 7b. Evaluate the candidate at the actual simulated fill. This keeps
        # spread/slippage inside the executable entry price and adds protocol
        # fees plus an explicit probability-uncertainty penalty.
        risk_decision_id: int | None = None
        if self._profile.risk_intelligence_policy != "off":
            from polysim.risk_intelligence.gates import (
                CorrelationContext,
                evaluate_trade_risk,
            )
            from polysim.risk_intelligence.scoring import extract_subject_key

            correlation = CorrelationContext(subject_key=extract_subject_key(market.question))
            if self._risk_intelligence is not None:
                correlation = await self._risk_intelligence.correlation_context(
                    run_id=self._run_id,
                    market_id=market_id,
                    market_question=market.question,
                )
            run = await dao.get_paper_run(self._db, self._run_id)
            starting_balance = int((run or {}).get("starting_balance_cents") or balance)
            spread_cents = None
            if book is not None and book.asks and book.bids:
                spread_cents = max(0, book.asks[0].price_cents - book.bids[0].price_cents)
            risk_decision = evaluate_trade_risk(
                policy=self._profile.risk_intelligence_policy,
                assessment=risk_assessment,
                correlation=correlation,
                outcome=outcome,
                shares=result.filled_size_shares,
                entry_price_cents=result.fill_price_cents,
                fee_cents=result.fee_cents,
                spread_cents=spread_cents,
                starting_balance_cents=starting_balance,
                max_correlated_positions=(self._profile.max_correlated_positions_per_subject),
                max_subject_exposure_pct=self._profile.max_subject_exposure_pct,
                min_source_quality=self._profile.risk_min_source_quality,
                max_rumor_risk=self._profile.risk_max_rumor_risk,
                max_information_asymmetry=(self._profile.risk_max_information_asymmetry),
                min_probability_confidence=(self._profile.risk_min_probability_confidence),
                min_edge_after_cost_cents=(self._profile.risk_min_edge_after_cost_cents),
                uncertainty_penalty_max_cents=(self._profile.risk_uncertainty_penalty_max_cents),
            )
            if self._risk_intelligence is not None:
                risk_decision_id = await self._risk_intelligence.record_decision(
                    run_id=self._run_id,
                    flag_id=int(flag.get("id") or flag_id),
                    market_id=market_id,
                    assessment=risk_assessment,
                    decision=risk_decision,
                )
            if not risk_decision.passed:
                log.info(
                    "risk gate: run #%d profile=%s skipped flag #%s - %s: %s",
                    self._run_id,
                    self._profile.name,
                    flag_id,
                    risk_decision.gate_name,
                    risk_decision.reason,
                )
                return None

        position_id = await dao.write_paper_position(
            self._db,
            run_id=self._run_id,
            market_id=market_id,
            outcome=outcome,
            size_shares=result.filled_size_shares,
            avg_entry_price_cents=result.fill_price_cents,
            source_flag_id=int(flag.get("id") or flag_id),
            source_wallet=source_wallet,
            signal_composite=signal_composite,
            signal_multiplier=signal_multiplier,
        )
        if risk_decision_id is not None and self._risk_intelligence is not None:
            await self._risk_intelligence.attach_position(risk_decision_id, position_id)
        # Settlement: §4.2 — every fill enters a 2-block pending window.
        from polysim.portfolio.settlement import (
            compute_settlement_window,
            record_settlement_start,
        )

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
        cost = result.filled_size_shares * result.fill_price_cents + result.fee_cents
        await self._inner.portfolio.apply_cost(-cost)

        log.info(
            "profile=%s run=%d opened position #%d  flag=%s  size=%d @ %d¢",
            self._profile.name,
            self._run_id,
            position_id,
            flag_id,
            result.filled_size_shares,
            result.fill_price_cents,
        )
        return position_id

    # ── resolution pass-through ──────────────────────────

    async def on_resolution(self, market_id: str) -> list[int]:
        return await self._inner.on_resolution(market_id)

    # ── addendum §5.1 stop-loss sweep ────────────────────

    async def sweep_stop_losses(self) -> list[int]:
        """Close any OPEN position whose mark-to-market dropped by
        `stop_loss_pct` below entry. Returns closed position ids.

        Profile must have stop_loss_enabled=True and stop_loss_pct set.
        Mark = newest available orderbook mid; falls back to last trade
        price for the (market, outcome) when no book snapshot is present.
        """
        if not self._profile.stop_loss_enabled:
            return []
        sl = self._profile.stop_loss_pct
        if sl is None or sl <= 0.0:
            return []

        opens = await dao.list_open_positions(self._db, self._run_id)
        closed: list[int] = []
        for pos in opens:
            try:
                pid = int(pos["id"])
                market_id = str(pos.get("market_id") or "")
                outcome = str(pos.get("outcome") or "")
                size = int(pos.get("size_shares") or 0)
                entry = int(pos.get("avg_entry_price_cents") or 0)
                if size <= 0 or entry <= 0:
                    continue

                mark = await _mark_price_cents(self._db, market_id, outcome)
                if mark is None:
                    continue
                # Loss fraction vs entry. Drops in price = unrealized loss.
                drop = max(0.0, (entry - mark) / entry)
                if drop < sl:
                    continue

                market = await dao.get_market(self._db, market_id)
                fee_cents = taker_fee_cents(
                    shares=size,
                    price_cents=mark,
                    category=(market.category if market else None),
                    metadata=(market.metadata if market else None),
                )
                realized = (mark - entry) * size - fee_cents
                proceeds = mark * size - fee_cents
                await self._inner.portfolio.apply_cost(proceeds)
                await dao.close_position(
                    self._db,
                    pid,
                    realized_pnl_cents=realized,
                    status="CLOSED",
                )
                await dao.write_paper_fill(
                    self._db,
                    run_id=self._run_id,
                    position_id=pid,
                    side="SELL",
                    size_shares=size,
                    fill_price_cents=mark,
                    intended_price_cents=mark,
                    slippage_cents=0,
                    latency_ms=0,
                    fee_cents=fee_cents,
                )
                closed.append(pid)
                log.info(
                    "stop-loss closed position #%d run=%d profile=%s "
                    "drop=%.1f%% (entry=%d mark=%d size=%d realized=%d)",
                    pid,
                    self._run_id,
                    self._profile.name,
                    drop * 100,
                    entry,
                    mark,
                    size,
                    realized,
                )
            except Exception as exc:
                log.warning("stop-loss sweep failed for position %s: %s", pos.get("id"), exc)
        return closed

    # ── external conversation signal (signals package) ──

    async def _signal_adjustment(self, market_id: str) -> tuple[bool, float | None, float | None]:
        """(blocked, multiplier, composite) for this market under the
        profile's signal policy. Fail-open: any error or missing/stale
        signal -> (False, None, None) - identical to signal_policy='off'.
        """
        from polysim.signals.scoring import (
            conviction_multiplier,
            signal_gate_blocks,
        )
        from polysim.signals.service import latest_market_signal

        try:
            sig = await latest_market_signal(
                self._db,
                market_id,
                max_age_hours=self._profile.signal_max_age_hours,
            )
        except Exception as exc:
            log.warning("signal lookup failed for %s: %s", market_id, exc)
            return False, None, None
        if sig is None:
            return False, None, None
        if self._profile.signal_policy == "gate_and_size" and signal_gate_blocks(
            sig,
            min_composite=self._profile.signal_gate_min_composite,
            min_confidence=self._profile.signal_gate_min_confidence,
        ):
            return True, None, sig.composite
        mult = conviction_multiplier(
            sig,
            min_mult=self._profile.signal_size_min_mult,
            max_mult=self._profile.signal_size_max_mult,
        )
        return False, mult, sig.composite

    # ── pre-gate: kill switches + drawdown + daily loss ──

    async def _pre_gate_tripped(self) -> bool:
        # Profile-provided limits override the bankroll defaults if present.
        max_dd = (
            self._profile.drawdown_limit_pct
            if self._profile.drawdown_limit_pct is not None
            else self._bankroll.kill_drawdown_pct
        )
        tripped = await kill_switches.check_and_pause(
            self._db,
            self._run_id,
            max_drawdown_pct=max_dd,
            max_flags_per_hour=self._bankroll.kill_flags_per_hour,
        )
        if tripped is not None:
            log.warning(
                "run #%d profile=%s paused by %s: %s",
                self._run_id,
                self._profile.name,
                tripped.code,
                tripped.detail,
            )
            return True

        # Daily-loss kill switch (profile-only).
        if self._profile.daily_loss_limit_pct is not None:
            tripped_daily = await daily_loss_switch(
                self._db,
                self._run_id,
                limit_pct=self._profile.daily_loss_limit_pct,
            )
            if tripped_daily is not None:
                await dao.pause_paper_run(
                    self._db,
                    self._run_id,
                    reason=f"daily_loss: {tripped_daily}",
                )
                log.warning(
                    "run #%d profile=%s paused by daily_loss: %s",
                    self._run_id,
                    self._profile.name,
                    tripped_daily,
                )
                return True

        # Any manual pause still counts.
        return bool(await self._inner.portfolio.is_paused())

    # ── filters ──────────────────────────────────────────

    def _contributing_ok(self, flag: dict[str, Any]) -> bool:
        if self._profile.allowed_detectors is None:
            return True
        components = flag.get("components_json") or flag.get("components") or "{}"
        if isinstance(components, str):
            import json

            try:
                comp = json.loads(components)
            except json.JSONDecodeError:
                comp = {}
        else:
            comp = components
        contributing = set(comp.get("contributing_detectors") or [])
        if not contributing:
            # The composite flag itself still carries its detector name.
            contributing = {str(flag.get("detector_name") or "")}
        return bool(contributing.intersection(self._profile.allowed_detectors))

    def _market_odds_ok(self, current_odds: float) -> bool:
        # Addendum spec: min_market_odds means "price is <= this" (cheap shots
        # only). max_market_odds means "price is >= this" (chalk only).
        if (
            self._profile.min_market_odds is not None
            and current_odds > self._profile.min_market_odds
        ):
            return False
        return not (
            self._profile.max_market_odds is not None
            and current_odds < self._profile.max_market_odds
        )

    def _market_volume_ok(self, daily_volume_cents: int | None) -> bool:
        v = int(daily_volume_cents or 0)
        if (
            self._profile.max_market_daily_volume_cents is not None
            and v > self._profile.max_market_daily_volume_cents
        ):
            return False
        return not (
            self._profile.min_market_daily_volume_cents is not None
            and v < self._profile.min_market_daily_volume_cents
        )

    async def _profile_portfolio_ok(
        self,
        *,
        balance: int,
        market_id: str,
        source_wallet: str,
        intended_notional: int,
    ) -> bool:
        # max_open_positions (profile override)
        open_positions = await self._inner.portfolio.open_positions()
        if len(open_positions) >= self._profile.max_open_positions:
            return False
        # per-wallet cap
        wallet_exp = sum(
            int(p.get("size_shares") or 0) * int(p.get("avg_entry_price_cents") or 0)
            for p in open_positions
            if str(p.get("source_wallet") or "").lower() == source_wallet.lower()
        )
        if wallet_exp + intended_notional > int(balance * self._profile.max_pct_per_source_wallet):
            return False
        # per-market cap
        market_exp = sum(
            int(p.get("size_shares") or 0) * int(p.get("avg_entry_price_cents") or 0)
            for p in open_positions
            if str(p.get("market_id") or "") == market_id
        )
        return market_exp + intended_notional <= int(balance * self._profile.max_pct_per_market)

    # ── sizing ───────────────────────────────────────────

    def _size_position(self, *, balance: int, flag: dict[str, Any], current_odds: float) -> int:
        mode = self._profile.position_sizing_mode
        if mode == "fixed":
            base = int(self._profile.fixed_copy_cents or 0)
            # Non-compounding profiles should never scale with balance;
            # compounding profiles can by using the starting balance ratio.
            if self._profile.compound_winnings:
                # simple compounding: scale by balance vs. fixed $50 assumed
                # on $10k starting -> factor = balance / 10k (bounded to 10x)
                factor = max(0.1, min(10.0, balance / 1_000_000))
                base = int(base * factor)
        elif mode == "percentage":
            base = int(balance * self._profile.max_pct_per_position)
        elif mode == "kelly_fractional":
            edge = _estimate_edge(float(flag.get("composite_score") or 0.0))
            if current_odds <= 0.0 or current_odds >= 1.0:
                kelly = 0.0
            else:
                # Kelly for a binary bet at price p with estimated true prob
                # p* = p + edge:  f* = (p* - p) / (1 - p) = edge / (1 - p)
                kelly = max(0.0, edge / (1.0 - current_odds))
            frac = self._profile.kelly_fraction or 0.25
            base = int(balance * kelly * frac)
        else:
            base = 0

        hard_cap = int(balance * self._profile.max_pct_per_position)
        return max(0, min(base, hard_cap))


def _estimate_edge(composite_score: float) -> float:
    """Map a composite score ∈ [0, 10] to an edge estimate.

    Simple linear map, clamped: 5.0 -> 0.05 (5 pp edge); 10.0 -> 0.30.
    This is deliberately modest; the bootstrap simulation is what exposes
    whether the edge assumption holds.
    """
    s = max(0.0, min(10.0, composite_score))
    if s <= 5.0:
        return 0.0
    return (s - 5.0) / 5.0 * 0.30


# ── mark price helper ────────────────────────────────────


async def _mark_price_cents(db_path: Path, market_id: str, outcome: str) -> int | None:
    """Best-effort mark price for an open position.

    Tries (1) newest orderbook snapshot mid; (2) last trade price for
    (market, outcome); returns None if neither is available.
    """
    import aiosqlite

    if not db_path.exists():
        return None
    # Orderbook mid (best bid + best ask)/2.
    try:
        async with aiosqlite.connect(str(db_path)) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT asks_json, bids_json FROM orderbook_snapshots "
                "WHERE market_id = ? AND outcome = ? ORDER BY timestamp DESC LIMIT 1",
                (market_id, outcome),
            ) as cur:
                row = await cur.fetchone()
        if row is not None:
            import json as _json

            try:
                asks = _json.loads(row["asks_json"])
                bids = _json.loads(row["bids_json"])
                if asks and bids:
                    best_ask = int(asks[0]["price_cents"])
                    best_bid = int(bids[0]["price_cents"])
                    return (best_ask + best_bid) // 2
            except (KeyError, ValueError, _json.JSONDecodeError):
                pass

        # Fallback: last trade price for this (market, outcome).
        async with (
            aiosqlite.connect(str(db_path)) as db,
            db.execute(
                "SELECT price_cents FROM trades WHERE market_id = ? AND outcome = ? "
                "ORDER BY timestamp DESC LIMIT 1",
                (market_id, outcome),
            ) as cur2,
        ):
            r2 = await cur2.fetchone()
        if r2 is not None and r2[0] is not None:
            return int(r2[0])
    except aiosqlite.OperationalError:
        return None
    return None


# ── daily-loss kill-switch helper ────────────────────────


async def daily_loss_switch(
    db_path: Path,
    run_id: int,
    *,
    limit_pct: float,
    now: datetime | None = None,
) -> str | None:
    """Trip when today's realized + intraday mark P&L exceeds `limit_pct`
    of the run's starting balance (loss side only).

    Returns a human-readable detail, or None if within limits.
    """
    now = now or now_utc()
    start_of_day = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    row = await dao.get_paper_run(db_path, run_id)
    if row is None:
        return None
    start = int(row.get("starting_balance_cents") or 0)
    if start <= 0:
        return None
    limit_cents = int(start * limit_pct)

    # Realized losses today: sum realized_pnl of positions closed today.
    import aiosqlite

    realized_today = 0
    if not db_path.exists():
        return None
    try:
        async with (
            aiosqlite.connect(str(db_path)) as db,
            db.execute(
                "SELECT COALESCE(SUM(realized_pnl_cents), 0) "
                "FROM paper_positions "
                "WHERE run_id = ? AND closed_at IS NOT NULL AND closed_at >= ?",
                (run_id, iso(start_of_day)),
            ) as cur,
        ):
            r = await cur.fetchone()
            realized_today = int(r[0]) if r and r[0] is not None else 0
    except aiosqlite.OperationalError:
        return None

    if -realized_today > limit_cents:
        return (
            f"realized loss today {-realized_today / 100:.2f}$ > "
            f"limit {limit_cents / 100:.2f}$ ({limit_pct:.1%} of start)"
        )
    return None


# ── Dispatcher (addendum §4.1) ───────────────────────────


class Dispatcher:
    """Fan-out one flag stream to N profile-aware executors.

    One flag may produce 0..N positions — one per run that accepts it.
    Each run has its own independent balance and positions.
    """

    def __init__(self, executors: list[ProfilePaperExecutor]) -> None:
        self._executors = executors

    @property
    def executors(self) -> list[ProfilePaperExecutor]:
        return self._executors

    async def on_flag(self, flag_id: int) -> dict[int, int | None]:
        """Fan a flag to every active executor. Paused runs get None.

        Returns {run_id -> new_position_id or None}.
        """
        import asyncio

        async def _per_run(ex: ProfilePaperExecutor) -> tuple[int, int | None]:
            row = await dao.get_paper_run(ex.inner._db, ex.run_id)
            if row is None:
                return ex.run_id, None
            if row.get("paused_at") or row.get("ended_at"):
                return ex.run_id, None
            pid = await ex.consider_flag(flag_id)
            return ex.run_id, pid

        results = await asyncio.gather(*(_per_run(e) for e in self._executors))
        return dict(results)

    async def on_resolution(self, market_id: str) -> dict[int, list[int]]:
        """Resolve across all runs."""
        import asyncio

        async def _per_run(ex: ProfilePaperExecutor) -> tuple[int, list[int]]:
            ids = await ex.on_resolution(market_id)
            return ex.run_id, ids

        results = await asyncio.gather(*(_per_run(e) for e in self._executors))
        return dict(results)


# Re-export helpers for tests.
__all__ = [
    "Dispatcher",
    "ProfilePaperExecutor",
    "daily_loss_switch",
]
