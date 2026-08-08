"""TimingDetector — spec §7.3.5 / build plan §2.7.

Signal: what % of a wallet's position in a market was opened in the last
`late_window_hours` before resolution. A wallet repeatedly piling in
right before resolution (and being right) looks informed.

Base-rate weighting (G5): the raw % is divided by the category's typical
late-activity base rate. Default base rate = 0.2 (20% of trades expected
in the final window) until `scripts/build_timing_baselines.py` writes
category-specific values into the `meta` table.
"""

from __future__ import annotations

import logging
import math
from datetime import timedelta
from pathlib import Path

from polysim.db import dao
from polysim.models import DetectorSignal, Market, TradeEvent, WalletProfile

log = logging.getLogger(__name__)

_DEFAULT_BASE_RATE = 0.2  # G5 default — replaced when meta-table loaded


class TimingDetector:
    name: str = "TimingDetector"

    def __init__(
        self,
        db_path: Path,
        *,
        late_window_hours: int = 1,
        base_rates_by_category: dict[str, float] | None = None,
    ) -> None:
        self._db = db_path
        self.late_window_hours = late_window_hours
        self._base_rates = base_rates_by_category or {}

    def base_rate_for(self, category: str | None) -> float:
        if category and category in self._base_rates:
            return self._base_rates[category]
        return _DEFAULT_BASE_RATE

    async def score(
        self,
        wallet: WalletProfile,
        market: Market,
        trade: TradeEvent | None,
    ) -> DetectorSignal | None:
        if market.resolves_at is None:
            return None  # can't define "late" without an end time
        late_cutoff = market.resolves_at - timedelta(hours=self.late_window_hours)

        try:
            trades = await dao.get_trades_by_wallet(self._db, wallet.wallet_address)
        except Exception as exc:
            log.warning("timing: db error for %s: %s", wallet.wallet_address, exc)
            return None
        market_trades = [t for t in trades if t.market_id == market.id]
        if not market_trades:
            return None

        total_shares = sum(t.size_shares for t in market_trades)
        if total_shares <= 0:
            return None
        late_shares = sum(
            t.size_shares for t in market_trades if t.timestamp >= late_cutoff
        )
        late_ratio = late_shares / total_shares

        base = self.base_rate_for(market.category)
        # Signal = how many times the base rate the late concentration is.
        # Apply tanh so large multipliers saturate.
        multiplier = late_ratio / max(1e-6, base)
        raw_score = math.tanh(max(0.0, multiplier - 1.0))
        if not math.isfinite(raw_score):
            return None
        raw_score = max(0.0, min(0.999, raw_score))

        return DetectorSignal(
            wallet_address=wallet.wallet_address,
            market_id=market.id,
            trade_id=trade.id if trade else None,
            detector_name=self.name,
            raw_score=raw_score,
            confidence=min(1.0, total_shares / 100.0),
            components={
                "late_ratio": late_ratio,
                "base_rate": base,
                "multiplier": multiplier,
            },
            evidence={
                "total_shares": float(total_shares),
                "late_shares": float(late_shares),
                "late_window_hours": float(self.late_window_hours),
                "category": market.category or "unknown",
            },
        )
