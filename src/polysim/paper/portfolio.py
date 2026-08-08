"""Portfolio — bankroll + cap enforcement for paper runs.

Spec §7.6 position-management rules:
  * Max 1 OPEN position per (run, market).
  * Max K open positions per run (bankroll.max_open_positions).
  * Per-wallet exposure <= max_pct_per_source_wallet of bankroll.
  * Per-market exposure <= max_pct_per_market of bankroll.
  * Per-position notional <= max_pct_per_position.

DB-backed: the source of truth is paper_runs / paper_positions / paper_fills.
Portfolio is a thin view + a few mutators that keep the two in sync.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from polysim.config import BankrollConfig
from polysim.db import dao

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class OpenReason:
    allowed: bool
    reason: str = ""


ALLOWED = OpenReason(allowed=True)


class Portfolio:
    def __init__(
        self,
        db_path: Path,
        *,
        run_id: int,
        bankroll: BankrollConfig,
    ) -> None:
        self._db = db_path
        self._run_id = run_id
        self._cfg = bankroll

    @property
    def run_id(self) -> int:
        return self._run_id

    # ── reads ────────────────────────────────────────────

    async def current_balance_cents(self) -> int:
        row = await dao.get_paper_run(self._db, self._run_id)
        if row is None:
            return 0
        return int(row.get("current_balance_cents") or 0)

    async def starting_balance_cents(self) -> int:
        row = await dao.get_paper_run(self._db, self._run_id)
        if row is None:
            return 0
        return int(row.get("starting_balance_cents") or 0)

    async def is_paused(self) -> bool:
        row = await dao.get_paper_run(self._db, self._run_id)
        return bool(row and row.get("paused_at"))

    async def open_positions(self) -> list[dict[str, Any]]:
        return await dao.list_open_positions(self._db, self._run_id)

    async def exposure_by_wallet(self, source_wallet: str) -> int:
        """Sum notional (size * avg_entry_price) of open positions from this wallet."""
        target = source_wallet.lower()
        total = 0
        for pos in await self.open_positions():
            if str(pos.get("source_wallet") or "").lower() == target:
                total += int(pos.get("size_shares") or 0) * int(
                    pos.get("avg_entry_price_cents") or 0
                )
        return total

    async def exposure_by_market(self, market_id: str) -> int:
        total = 0
        for pos in await self.open_positions():
            if str(pos.get("market_id") or "") == market_id:
                total += int(pos.get("size_shares") or 0) * int(
                    pos.get("avg_entry_price_cents") or 0
                )
        return total

    # ── caps gatekeeper ──────────────────────────────────

    async def can_open(
        self,
        *,
        market_id: str,
        source_wallet: str | None,
        intended_notional_cents: int,
    ) -> OpenReason:
        """Spec §7.6 — enforce all caps before a position opens."""
        if await self.is_paused():
            return OpenReason(False, "run paused by kill switch")

        balance = await self.current_balance_cents()
        if balance <= 0:
            return OpenReason(False, f"current balance non-positive ({balance})")

        # Empirical-priors §4.2: subtract capital locked in pending-
        # settlement positions from available balance for cap checks.
        from polysim.portfolio.settlement import pending_capacity_cents
        pending = await pending_capacity_cents(self._db, self._run_id)
        effective_balance = max(0, balance - pending)
        if effective_balance <= 0:
            return OpenReason(
                False,
                f"all capital pending settlement "
                f"(balance={balance} pending={pending})",
            )

        open_positions = await self.open_positions()
        if len(open_positions) >= self._cfg.max_open_positions:
            return OpenReason(
                False, f"max_open_positions ({self._cfg.max_open_positions}) reached"
            )

        # Max 1 open position per (run, market).
        for pos in open_positions:
            if str(pos.get("market_id") or "") == market_id:
                return OpenReason(
                    False, f"existing OPEN position for market {market_id}"
                )

        # Per-position cap
        max_pos_cents = int(balance * self._cfg.max_pct_per_position)
        if intended_notional_cents > max_pos_cents:
            return OpenReason(
                False,
                f"position size {intended_notional_cents} > max_pct_per_position "
                f"({max_pos_cents})",
            )

        # Per-market cap (including this new position)
        market_exposure = await self.exposure_by_market(market_id)
        max_market_cents = int(balance * self._cfg.max_pct_per_market)
        if market_exposure + intended_notional_cents > max_market_cents:
            return OpenReason(
                False,
                f"market exposure {market_exposure + intended_notional_cents} > "
                f"max_pct_per_market ({max_market_cents})",
            )

        # Per-wallet cap
        if source_wallet:
            wallet_exposure = await self.exposure_by_wallet(source_wallet)
            max_wallet_cents = int(balance * self._cfg.max_pct_per_source_wallet)
            if wallet_exposure + intended_notional_cents > max_wallet_cents:
                return OpenReason(
                    False,
                    f"wallet exposure {wallet_exposure + intended_notional_cents} > "
                    f"max_pct_per_source_wallet ({max_wallet_cents})",
                )

        return ALLOWED

    # ── writes ───────────────────────────────────────────

    async def apply_cost(self, delta_cents: int) -> int:
        """Apply a signed delta to the run balance. Returns new balance."""
        return await dao.adjust_run_balance(self._db, self._run_id, delta_cents)

    async def close_position_at_payout(
        self,
        position_id: int,
        *,
        payout_per_share_cents: int,
        invalid: bool = False,
    ) -> int:
        """Spec §9 — resolution: $1 per winning share, $0 losing, $0 invalid.
        Returns realized_pnl_cents."""
        pos = await dao.get_position(self._db, position_id)
        if pos is None:
            log.warning("close_position_at_payout: position #%d missing", position_id)
            return 0

        size = int(pos.get("size_shares") or 0)
        avg_entry = int(pos.get("avg_entry_price_cents") or 0)

        realized = 0 if invalid else size * (payout_per_share_cents - avg_entry)

        # Refund the collateral + realized P&L into bankroll.
        # Cost at open was size * avg_entry (already deducted).
        # At close: credit size * payout. Net = payout.
        credit = size * payout_per_share_cents if not invalid else size * avg_entry
        # NOTE: on invalid, we refund the entry cost back (§9 "realized_pnl = 0").
        await self.apply_cost(credit)
        await dao.close_position(
            self._db, position_id, realized_pnl_cents=realized, status="RESOLVED"
        )
        return realized
