"""Order-book depth-aware sizing — empirical-priors addendum §4.4.

The arbitrage paper caps at 50% of available depth; Kaustubh and Lunar
both note that position > book depth moves the market against the trader.
We cap stricter:

  * Systematic mode: ≤ 25% of top-3-levels depth
  * Degen mode:      ≤ 40% of top-3-levels depth

If the intended size exceeds the cap, the executor either splits across
cycles or sizes down — never single-shots through depth.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite

log = logging.getLogger(__name__)

# Per addendum §4.4.
DEFAULT_SYSTEMATIC_DEPTH_PCT = 0.25
DEFAULT_DEGEN_DEPTH_PCT = 0.40


@dataclass(frozen=True)
class DepthCheck:
    side: str                          # "BUY" → look at asks; "SELL" → bids
    top_n_levels: int                  # how many levels we summed
    available_shares: int              # sum of size at top-N levels
    cap_shares: int                    # available * pct (the legal max)
    requested_shares: int
    allowed: bool                      # requested ≤ cap
    pct_used: float                    # for logging


def top_n_size_shares(book_levels: list[dict[str, Any]], n: int = 3) -> int:
    """Sum size_shares at the top `n` levels of one side of a book.

    `book_levels` is the deserialized JSON list in `orderbook_snapshots`.
    Robust to malformed entries.
    """
    if not isinstance(book_levels, list):
        return 0
    total = 0
    for lvl in book_levels[:n]:
        if not isinstance(lvl, dict):
            continue
        try:
            total += int(lvl.get("size_shares") or 0)
        except (TypeError, ValueError):
            continue
    return max(0, total)


def check_depth(
    *,
    side: str,
    requested_shares: int,
    bids: list[dict[str, Any]] | None,
    asks: list[dict[str, Any]] | None,
    cap_pct: float = DEFAULT_SYSTEMATIC_DEPTH_PCT,
    top_n: int = 3,
) -> DepthCheck:
    """Pure: does `requested_shares` exceed `cap_pct` of top-N depth?"""
    side_u = side.strip().upper()
    if side_u == "BUY":
        levels = asks or []
    elif side_u == "SELL":
        levels = bids or []
    else:
        levels = []
    available = top_n_size_shares(levels, n=top_n)
    cap = int(available * float(cap_pct))
    allowed = requested_shares <= cap if cap > 0 else False
    pct_used = (requested_shares / available) if available > 0 else float("inf")
    return DepthCheck(
        side=side_u, top_n_levels=top_n,
        available_shares=available, cap_shares=cap,
        requested_shares=requested_shares,
        allowed=allowed, pct_used=pct_used,
    )


def cap_size_to_depth(
    requested_shares: int,
    *,
    side: str,
    bids: list[dict[str, Any]] | None,
    asks: list[dict[str, Any]] | None,
    cap_pct: float = DEFAULT_SYSTEMATIC_DEPTH_PCT,
    top_n: int = 3,
) -> int:
    """If `requested_shares` exceeds the cap, return the cap; else return
    requested. Returns 0 when there is no usable depth.
    """
    chk = check_depth(
        side=side, requested_shares=requested_shares,
        bids=bids, asks=asks, cap_pct=cap_pct, top_n=top_n,
    )
    if chk.available_shares <= 0:
        return 0
    if chk.allowed:
        return requested_shares
    return chk.cap_shares


async def latest_book_for(
    db_path: Path, *, market_id: str, outcome: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]] | None:
    """Newest orderbook snapshot for (market, outcome), or None."""
    if not db_path.exists():
        return None
    try:
        async with aiosqlite.connect(str(db_path)) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT asks_json, bids_json FROM orderbook_snapshots "
                "WHERE market_id = ? AND outcome = ? "
                "ORDER BY timestamp DESC LIMIT 1",
                (market_id, outcome),
            ) as cur:
                row = await cur.fetchone()
    except aiosqlite.OperationalError:
        return None
    if row is None:
        return None
    try:
        asks = json.loads(row["asks_json"])
        bids = json.loads(row["bids_json"])
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(asks, list) or not isinstance(bids, list):
        return None
    return bids, asks


__all__ = [
    "DEFAULT_DEGEN_DEPTH_PCT",
    "DEFAULT_SYSTEMATIC_DEPTH_PCT",
    "DepthCheck",
    "cap_size_to_depth",
    "check_depth",
    "latest_book_for",
    "top_n_size_shares",
]
