"""EventInsiderDetector — spec §7.3.2 / build plan §2.4.

Fires on a single trade. Four features:
  1. Fresh wallet:   nonce < FRESH_NONCE_THRESHOLD
  2. Large size:     size_cents > FRESH_SIZE_MIN_CENTS
  3. Niche market:   daily_volume_cents < NICHE_MARKET_VOL_MAX
  4. Contrarian:     |price - mid| >= CONTRARIAN_BPS
       where "mid" is an estimate (caller-supplied reference_mid_cents;
       defaults to 50 when unknown).

All four hard gates must pass for a non-zero score. Scoring is a logistic
combination of normalized features — see `_logistic_combo`. Monotonically
increases with any single feature becoming "more insider-like".
"""

from __future__ import annotations

import math

from polysim.models import DetectorSignal, Market, TradeEvent, WalletProfile


class EventInsiderDetector:
    name: str = "EventInsiderDetector"

    def __init__(
        self,
        *,
        fresh_nonce_threshold: int = 10,
        fresh_size_min_cents: int = 500_000,
        niche_market_vol_max_cents: int = 50_000_000,
        contrarian_bps: int = 1500,
    ) -> None:
        self.fresh_nonce_threshold = fresh_nonce_threshold
        self.fresh_size_min_cents = fresh_size_min_cents
        self.niche_market_vol_max_cents = niche_market_vol_max_cents
        self.contrarian_bps = contrarian_bps

    async def score(
        self,
        wallet: WalletProfile,
        market: Market,
        trade: TradeEvent | None,
    ) -> DetectorSignal | None:
        if trade is None:
            return None

        nonce = int(wallet.features.get("nonce", 0) or 0)
        size_cents = trade.size_shares * trade.price_cents
        market_vol = market.daily_volume_usd_cents or 0
        reference_mid_cents = int(wallet.features.get("market_mid_cents", 50) or 50)
        contrarian_pts = abs(trade.price_cents - reference_mid_cents)
        contrarian_bps = contrarian_pts * 100  # 1 cent = 100 bps

        # Hard gates — all must pass.
        if nonce >= self.fresh_nonce_threshold:
            return None
        if size_cents <= self.fresh_size_min_cents:
            return None
        if market_vol <= 0 or market_vol >= self.niche_market_vol_max_cents:
            return None
        if contrarian_bps < self.contrarian_bps:
            return None

        # Feature normalization (each in [0,1], larger = more insider-like).
        nonce_f = 1.0 - (nonce / self.fresh_nonce_threshold)
        size_f = _saturating_ratio(size_cents - self.fresh_size_min_cents, self.fresh_size_min_cents)
        vol_f = 1.0 - (market_vol / self.niche_market_vol_max_cents)
        contr_f = _saturating_ratio(contrarian_bps - self.contrarian_bps, self.contrarian_bps)

        raw_score = _logistic_combo(nonce_f, size_f, vol_f, contr_f)
        if not math.isfinite(raw_score):
            return None
        raw_score = max(0.0, min(0.999, raw_score))

        return DetectorSignal(
            wallet_address=wallet.wallet_address,
            market_id=market.id,
            trade_id=trade.id,
            detector_name=self.name,
            raw_score=raw_score,
            confidence=0.9,
            components={
                "nonce_feature": nonce_f,
                "size_feature": size_f,
                "volume_feature": vol_f,
                "contrarian_feature": contr_f,
            },
            evidence={
                "nonce": float(nonce),
                "size_cents": float(size_cents),
                "market_volume_cents": float(market_vol),
                "contrarian_bps": float(contrarian_bps),
                "reference_mid_cents": float(reference_mid_cents),
            },
        )


def _saturating_ratio(numerator: float, denominator: float) -> float:
    """Return min(1, max(0, numerator / denominator)). 0 if denom <= 0."""
    if denominator <= 0:
        return 0.0
    return max(0.0, min(1.0, numerator / denominator))


def _logistic_combo(*features: float) -> float:
    """Logistic of a weighted feature sum, rescaled to cover [0, 1] when
    all features are 0..1. Weighted equally.

    At all-zero features → output ≈ 0.15; at all-one → ≈ 0.90.
    Monotonically increasing in each feature.
    """
    if not features:
        return 0.0
    # Weighted sum; scale so that 4 equal features in [0,1] span a reasonable
    # logit range roughly centered at the midpoint.
    weighted = sum(features) / len(features)
    # Logit transform around 0.4 (slightly generous at the midpoint).
    z = 6.0 * (weighted - 0.4)
    return 1.0 / (1.0 + math.exp(-z))
