"""Equity price data layer — Yahoo chart JSON (no key), cached to equity_quotes.

Prices are stored in integer cents (close $214.25 -> 21425). Exposes
indicator helpers (SMA / ATR / trailing return) computed off the cache.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite
import httpx

from polysim.utils.time import iso, now_utc

log = logging.getLogger(__name__)

YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/"
_UA = {"User-Agent": "Mozilla/5.0 (polysim equity)"}


async def fetch_daily(
    client: httpx.AsyncClient, ticker: str, *, rng: str = "1y"
) -> list[dict[str, Any]]:
    """Return [{date, open, high, low, close, volume}] in cents, oldest first."""
    try:
        r = await client.get(
            f"{YAHOO}{ticker.upper()}",
            params={"range": rng, "interval": "1d"},
            headers=_UA,
            timeout=40,
        )
        if r.status_code != 200:
            return []
        res = r.json()["chart"]["result"][0]
    except Exception as exc:
        log.warning("equity price fetch failed for %s: %s", ticker, exc)
        return []
    ts = res.get("timestamp") or []
    q = (res.get("indicators", {}).get("quote") or [{}])[0]
    o, h, lo, c = (q.get("open") or [], q.get("high") or [],
                   q.get("low") or [], q.get("close") or [])
    v = q.get("volume") or []
    out: list[dict[str, Any]] = []
    for i, t in enumerate(ts):
        if i >= len(c) or c[i] is None or i >= len(o) or o[i] is None:
            continue
        d = datetime.fromtimestamp(t, tz=UTC).strftime("%Y-%m-%d")
        out.append({
            "date": d,
            "open": round(float(o[i]) * 100),
            "high": round(float(h[i]) * 100) if i < len(h) and h[i] else round(float(c[i]) * 100),
            "low": round(float(lo[i]) * 100) if i < len(lo) and lo[i] else round(float(c[i]) * 100),
            "close": round(float(c[i]) * 100),
            "volume": int(v[i]) if i < len(v) and v[i] else 0,
        })
    return out


async def cache_quotes(db_path: Path, ticker: str, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    n = iso(now_utc())
    async with aiosqlite.connect(str(db_path)) as db:
        await db.executemany(
            """
            INSERT INTO equity_quotes(
                ticker, date, open_cents, high_cents, low_cents,
                close_cents, volume, source, fetched_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'yahoo', ?)
            ON CONFLICT(ticker, date) DO UPDATE SET
                open_cents=excluded.open_cents, high_cents=excluded.high_cents,
                low_cents=excluded.low_cents, close_cents=excluded.close_cents,
                volume=excluded.volume, fetched_at=excluded.fetched_at
            """,
            [(ticker.upper(), r["date"], r["open"], r["high"], r["low"],
              r["close"], r["volume"], n) for r in rows],
        )
        await db.commit()
    return len(rows)


async def refresh_universe(
    db_path: Path, tickers: list[str], *, rng: str = "1y"
) -> int:
    """Fetch + cache daily quotes for every ticker. Returns rows written."""
    total = 0
    async with httpx.AsyncClient() as client:
        for t in tickers:
            rows = await fetch_daily(client, t, rng=rng)
            total += await cache_quotes(db_path, t, rows)
    return total


async def closes(db_path: Path, ticker: str, *, limit: int = 260) -> list[int]:
    """Most-recent `limit` daily closes (cents), oldest first."""
    async with aiosqlite.connect(str(db_path)) as db, db.execute(
        "SELECT close_cents FROM equity_quotes WHERE ticker = ? "
        "ORDER BY date DESC LIMIT ?",
        (ticker.upper(), limit),
    ) as cur:
        rows = await cur.fetchall()
    return [int(r[0]) for r in reversed(list(rows))]


async def latest_close(db_path: Path, ticker: str) -> tuple[str, int] | None:
    async with aiosqlite.connect(str(db_path)) as db, db.execute(
        "SELECT date, close_cents FROM equity_quotes WHERE ticker = ? "
        "ORDER BY date DESC LIMIT 1",
        (ticker.upper(),),
    ) as cur:
        row = await cur.fetchone()
    return (str(row[0]), int(row[1])) if row else None


async def ohlc_window(
    db_path: Path, ticker: str, *, limit: int = 30
) -> list[dict[str, int]]:
    async with aiosqlite.connect(str(db_path)) as db, db.execute(
        "SELECT high_cents, low_cents, close_cents FROM equity_quotes "
        "WHERE ticker = ? ORDER BY date DESC LIMIT ?",
        (ticker.upper(), limit),
    ) as cur:
        rows = await cur.fetchall()
    return [{"high": int(r[0]), "low": int(r[1]), "close": int(r[2])}
            for r in reversed(list(rows))]


# ── indicators (pure) ────────────────────────────────────────

def sma(vals: list[int], n: int) -> float | None:
    return sum(vals[-n:]) / n if len(vals) >= n else None


def trailing_return(vals: list[int], lookback: int) -> float | None:
    if len(vals) <= lookback or vals[-1 - lookback] == 0:
        return None
    return vals[-1] / vals[-1 - lookback] - 1.0


def atr(window: list[dict[str, int]], n: int = 14) -> float | None:
    """Average true range in cents from an OHLC window (close-based TR)."""
    if len(window) < n + 1:
        return None
    trs: list[float] = []
    for i in range(1, len(window)):
        hi, lo = window[i]["high"], window[i]["low"]
        pc = window[i - 1]["close"]
        trs.append(max(hi - lo, abs(hi - pc), abs(lo - pc)))
    return sum(trs[-n:]) / n if len(trs) >= n else None
