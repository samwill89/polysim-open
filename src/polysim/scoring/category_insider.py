"""CategoryInsiderDetector — spec §7.3.1 / build plan §2.3.

Binomial p-value of a wallet's win rate within a single category against a
reference base rate (default 0.5 — an outcome-blind trader's expected hit
rate). When ≥ `min_resolved_markets` have settled, a wallet significantly
outperforming chance is flagged.

We use `scipy.stats.binomtest(...).pvalue` with the one-sided greater
alternative; score = 1 - p, capped at 0.999. Lower-bound of confidence
grows with n (more evidence → higher confidence weight), approximated
with `min(1, n / 4*min_resolved)`.
"""

from __future__ import annotations

import math

from scipy.stats import binomtest

from polysim.models import DetectorSignal, Market, TradeEvent, WalletProfile


class CategoryInsiderDetector:
    name: str = "CategoryInsiderDetector"

    def __init__(
        self,
        *,
        min_resolved_markets: int = 8,
        base_rate: float = 0.5,
    ) -> None:
        if not 0.0 < base_rate < 1.0:
            raise ValueError(f"base_rate must be in (0,1), got {base_rate}")
        if min_resolved_markets < 1:
            raise ValueError("min_resolved_markets must be >= 1")
        self.min_resolved_markets = min_resolved_markets
        self.base_rate = base_rate

    async def score(
        self,
        wallet: WalletProfile,
        market: Market,
        trade: TradeEvent | None,
    ) -> DetectorSignal | None:
        cat = market.category
        if cat is None:
            return None
        trades_in_cat = int(wallet.categories.get(cat, 0))
        if trades_in_cat < self.min_resolved_markets:
            return None

        # Approximation: we don't split wins/losses per category in the
        # profile schema. Assume wallet's per-category win rate ≈ overall
        # win_rate when trades_in_cat / total is significant. This is a
        # known loss-of-fidelity that Phase 2 accepts; a per-category
        # breakdown lands in the features_json block if needed.
        settled = wallet.wins + wallet.losses
        if settled == 0:
            return None
        wins = round(wallet.win_rate * trades_in_cat)
        wins = max(0, min(trades_in_cat, wins))
        p_value = binomtest(
            k=wins, n=trades_in_cat, p=self.base_rate, alternative="greater"
        ).pvalue
        if not math.isfinite(p_value):
            return None
        raw_score = min(0.999, 1.0 - float(p_value))
        # Confidence saturates to 1.0 once we have 2x the minimum resolved
        # markets in category — matches the binomial power curve where
        # doubling n sharply reduces false-positive risk.
        confidence = min(1.0, trades_in_cat / (2 * self.min_resolved_markets))

        return DetectorSignal(
            wallet_address=wallet.wallet_address,
            market_id=market.id,
            trade_id=trade.id if trade else None,
            detector_name=self.name,
            raw_score=raw_score,
            confidence=confidence,
            components={
                "wins": float(wins),
                "trades_in_category": float(trades_in_cat),
                "p_value": float(p_value),
                "base_rate": self.base_rate,
            },
            evidence={
                "category": cat,
                "overall_win_rate": wallet.win_rate,
                "min_resolved_markets": self.min_resolved_markets,
            },
        )
