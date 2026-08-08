"""Detector protocol — the interface every detector implements. Spec §7.3."""

from __future__ import annotations

from typing import Protocol

from polysim.models import DetectorSignal, Market, TradeEvent, WalletProfile


class Detector(Protocol):
    """Structural protocol — detectors don't need to inherit, just match."""

    name: str

    async def score(
        self,
        wallet: WalletProfile,
        market: Market,
        trade: TradeEvent | None,
    ) -> DetectorSignal | None:
        """Return a signal, or None when evidence is insufficient."""
        ...
