"""Free forward sentiment ingestion.

Two sources confirmed reachable from the host:
  - ApeWisdom: aggregate Reddit (WSB/stocks/...) mention-volume + 24h change.
    This is the ATTENTION signal — the research's strongest predictor.
  - StockTwits: per-symbol message streams tagged Bullish/Bearish — the STANCE
    signal (polarity), used only to set direction.

Reddit's own JSON 403s from datacenter IPs, but ApeWisdom already aggregates
it. Specific X accounts (Citrini etc.) need paid access; `ingest_x` is a
stub adapter slot for when that's wired.

Everything writes raw snapshots to equity_sentiment; the daily signal builder
aggregates them.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import aiosqlite
import httpx

from polysim.utils.time import iso, now_utc

log = logging.getLogger(__name__)

APEWISDOM = "https://apewisdom.io/api/v1.0/filter/all-stocks/page/"
STOCKTWITS = "https://api.stocktwits.com/api/2/streams/symbol/"
_UA = {"User-Agent": "Mozilla/5.0 (polysim equity research)"}


async def ingest_apewisdom(
    db_path: Path, universe: set[str], *, pages: int = 2
) -> int:
    """Pull top mention-volume tickers; keep those in our universe."""
    rows: list[tuple[Any, ...]] = []
    ts = iso(now_utc())
    async with httpx.AsyncClient(headers=_UA) as client:
        for page in range(1, pages + 1):
            try:
                r = await client.get(f"{APEWISDOM}{page}", timeout=30)
                if r.status_code != 200:
                    continue
                results = r.json().get("results", [])
            except Exception as exc:
                log.warning("apewisdom page %d failed: %s", page, exc)
                continue
            for x in results:
                tk = str(x.get("ticker", "")).upper()
                if tk not in universe:
                    continue
                mentions = int(x.get("mentions") or 0)
                prev = x.get("mentions_24h_ago")
                chg = None
                if prev not in (None, 0):
                    chg = (mentions - int(prev)) / int(prev)
                rows.append((
                    ts, tk, "apewisdom", mentions, chg,
                    int(x.get("upvotes") or 0), 0, 0,
                    int(x.get("rank") or 0) or None,
                    json.dumps({k: x.get(k) for k in ("name", "rank_24h_ago")}),
                ))
    return await _write(db_path, rows)


async def ingest_stocktwits(db_path: Path, tickers: list[str]) -> int:
    """Per-symbol Bullish/Bearish tag counts from the recent message stream."""
    rows: list[tuple[Any, ...]] = []
    ts = iso(now_utc())
    async with httpx.AsyncClient(headers=_UA) as client:
        for tk in tickers:
            try:
                r = await client.get(f"{STOCKTWITS}{tk.upper()}.json", timeout=30)
                if r.status_code != 200:
                    continue
                msgs = r.json().get("messages", [])
            except Exception:
                continue
            bull = bear = 0
            for m in msgs:
                s = (((m.get("entities") or {}).get("sentiment")) or {})
                basic = str(s.get("basic") or "").lower()
                if basic == "bullish":
                    bull += 1
                elif basic == "bearish":
                    bear += 1
            if bull or bear or msgs:
                rows.append((
                    ts, tk.upper(), "stocktwits", len(msgs), None, 0,
                    bull, bear, None, None,
                ))
    return await _write(db_path, rows)


async def ingest_x(db_path: Path, accounts: list[str]) -> int:
    """Adapter slot for specific-account ingestion (Citrini etc.).

    Requires paid X access (pay-per-use ~$10-40/mo) or an Apify scrape; left
    as a no-op until that data source is provisioned.
    """
    log.info("equity: X account ingestion not provisioned (needs paid access)")
    return 0


async def ingest_all(
    db_path: Path, allowlist: set[str], active_tickers: list[str]
) -> dict[str, int]:
    """ApeWisdom is filtered to the broad allowlist (so new names can be
    discovered + promoted); StockTwits polls only the active set (bounded)."""
    ape = await ingest_apewisdom(db_path, allowlist)
    st = await ingest_stocktwits(db_path, active_tickers)
    log.info("equity sentiment ingest: apewisdom=%d stocktwits=%d", ape, st)
    return {"apewisdom": ape, "stocktwits": st}


async def _write(db_path: Path, rows: list[tuple[Any, ...]]) -> int:
    if not rows:
        return 0
    async with aiosqlite.connect(str(db_path)) as db:
        await db.executemany(
            """
            INSERT INTO equity_sentiment(
                ts, ticker, source, mentions, mention_change_pct,
                upvotes, bull_count, bear_count, rank, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        await db.commit()
    return len(rows)
