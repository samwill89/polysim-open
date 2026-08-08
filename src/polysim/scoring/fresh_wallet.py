"""FreshWalletDetector — spec §7.3.3 / build plan §2.5.

Softer signal than EventInsiderDetector: scores on how fresh the wallet
is and how large the trade is, without requiring contrarian direction or
niche market volume. **G11**: this detector scores only; it never flags
alone. The composite scorer's `min_contributing_detectors >= 2` rule is
what prevents lone-detector flagging.
"""

from __future__ import annotations

import math

from polysim.models import DetectorSignal, Market, TradeEvent, WalletProfile


class FreshWalletDetector:
    name: str = "FreshWalletDetector"

    def __init__(
        self,
        *,
        fresh_nonce_threshold: int = 10,
        size_reference_cents: int = 50_000,  # $500 — much lower than EventInsider
    ) -> None:
        self.fresh_nonce_threshold = fresh_nonce_threshold
        self.size_reference_cents = size_reference_cents

    async def score(
        self,
        wallet: WalletProfile,
        market: Market,
        trade: TradeEvent | None,
    ) -> DetectorSignal | None:
        if trade is None:
            return None
        nonce = int(wallet.features.get("nonce", 0) or 0)
        if nonce >= self.fresh_nonce_threshold:
            return None

        # Freshness is a graded signal: smaller nonce → higher.
        freshness = 1.0 - (nonce / self.fresh_nonce_threshold)
        size_cents = trade.size_shares * trade.price_cents
        size_factor = 1.0 - math.exp(-size_cents / max(1, self.size_reference_cents))

        raw_score = freshness * size_factor
        if not math.isfinite(raw_score):
            return None
        raw_score = max(0.0, min(0.999, raw_score))

        return DetectorSignal(
            wallet_address=wallet.wallet_address,
            market_id=market.id,
            trade_id=trade.id,
            detector_name=self.name,
            raw_score=raw_score,
            confidence=0.7,  # softer detector → lower confidence
            components={
                "freshness": freshness,
                "size_factor": size_factor,
            },
            evidence={
                "nonce": float(nonce),
                "size_cents": float(size_cents),
            },
        )
