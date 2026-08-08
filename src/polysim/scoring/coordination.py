"""CoordinationDetector — spec §7.3.4 / build plan §2.6.

When multiple fresh wallets converge on the same low-volume market within
a rolling window (G4 default: 24h), that's a coordination signal. We
query the DB for co-movers on demand rather than maintain a persistent
NetworkX graph — simpler, stateless, and plenty fast at PolySim's rates.

Edge-weight heuristic = count * (1 / market_volume_cents).
Score = tanh(total_edge_weight_from_this_wallet / reference) capped 0.999.
"""

from __future__ import annotations

import logging
import math
from datetime import timedelta
from pathlib import Path

from polysim.db import dao
from polysim.models import DetectorSignal, Market, TradeEvent, WalletProfile
from polysim.utils.time import iso, now_utc

log = logging.getLogger(__name__)


class CoordinationDetector:
    name: str = "CoordinationDetector"

    def __init__(
        self,
        db_path: Path,
        *,
        window_hours: int = 24,
        niche_vol_cents: int = 50_000_000,
        edge_weight_reference: float = 1.0,
    ) -> None:
        self._db = db_path
        self.window_hours = window_hours
        self.niche_vol_cents = niche_vol_cents
        self.edge_weight_reference = edge_weight_reference

    async def score(
        self,
        wallet: WalletProfile,
        market: Market,
        trade: TradeEvent | None,
    ) -> DetectorSignal | None:
        # Only meaningful on low-volume markets — in deep markets there
        # are thousands of co-movers by chance.
        if market.daily_volume_usd_cents is None or market.daily_volume_usd_cents <= 0:
            return None
        if market.daily_volume_usd_cents >= self.niche_vol_cents:
            return None

        since_dt = now_utc() - timedelta(hours=self.window_hours)
        since_iso = iso(since_dt)
        try:
            trades_in_market = await dao.get_trades_by_market(
                self._db, market.id, since=since_iso
            )
        except Exception as exc:
            log.warning("coordination: db error on %s: %s", market.id, exc)
            return None

        peer_wallets: set[str] = set()
        for t in trades_in_market:
            addr = t.wallet_address.lower()
            if addr != wallet.wallet_address.lower():
                peer_wallets.add(addr)
        if not peer_wallets:
            return None

        # Edge weight from this wallet = num_peers * (1 / volume_cents)
        # Normalize the "1 / volume" to roughly [0,1] using the niche vol cap.
        vol_factor = self.niche_vol_cents / float(market.daily_volume_usd_cents)
        peer_factor = math.log(1 + len(peer_wallets))  # diminishing with count
        edge_weight = peer_factor * vol_factor

        raw_score = math.tanh(edge_weight / self.edge_weight_reference)
        if not math.isfinite(raw_score):
            return None
        raw_score = max(0.0, min(0.999, raw_score))

        return DetectorSignal(
            wallet_address=wallet.wallet_address,
            market_id=market.id,
            trade_id=trade.id if trade else None,
            detector_name=self.name,
            raw_score=raw_score,
            confidence=min(1.0, len(peer_wallets) / 5.0),
            components={
                "peer_count": float(len(peer_wallets)),
                "volume_factor": vol_factor,
                "edge_weight": edge_weight,
            },
            evidence={
                "window_hours": float(self.window_hours),
                "market_volume_cents": float(market.daily_volume_usd_cents),
            },
        )
