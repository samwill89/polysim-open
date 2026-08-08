"""Daily per-ticker composite signal.

attention-led, polarity-directed (per the evidence: mention-volume predicts,
raw opinion mostly sets direction). Composite, clipped to [-3, 3]:

    0.5 * z_attn * dir   (attention z-score, signed by stance/+1)
  + 0.3 * stance         (credibility-weighted polarity, here ST bull/bear)
  + 0.1 * novelty        (first credible mention in 10 trading days)
  - 0.1 * disagreement   (1 - |stance|; conflicting opinion shrinks conviction)

Writes one row per (date, ticker) to equity_signals.
"""

from __future__ import annotations

import json
import logging
import statistics
from pathlib import Path
from typing import Any

import aiosqlite

from polysim.utils.time import iso, now_utc

log = logging.getLogger(__name__)


async def _daily_mentions(
    db: aiosqlite.Connection, ticker: str, *, days: int = 40
) -> list[int]:
    """One mention count per day (max apewisdom snapshot), oldest first."""
    async with db.execute(
        "SELECT substr(ts,1,10) d, MAX(mentions) m FROM equity_sentiment "
        "WHERE ticker = ? AND source = 'apewisdom' "
        "GROUP BY d ORDER BY d DESC LIMIT ?",
        (ticker, days),
    ) as cur:
        rows = await cur.fetchall()
    return [int(r[1]) for r in reversed(list(rows))]


async def _latest_stance(
    db: aiosqlite.Connection, ticker: str
) -> tuple[float, int]:
    """(stance in [-1,1], total messages) from the newest StockTwits snapshot."""
    async with db.execute(
        "SELECT bull_count, bear_count FROM equity_sentiment "
        "WHERE ticker = ? AND source = 'stocktwits' "
        "ORDER BY ts DESC LIMIT 1",
        (ticker,),
    ) as cur:
        row = await cur.fetchone()
    if not row:
        return 0.0, 0
    bull, bear = int(row[0]), int(row[1])
    tot = bull + bear
    return ((bull - bear) / tot if tot else 0.0), tot


async def _days_since_prior_mention(
    db: aiosqlite.Connection, ticker: str, today: str
) -> int:
    async with db.execute(
        "SELECT COUNT(DISTINCT substr(ts,1,10)) FROM equity_sentiment "
        "WHERE ticker = ? AND source = 'apewisdom' AND mentions > 0 "
        "AND substr(ts,1,10) < ?",
        (ticker, today),
    ) as cur:
        row = await cur.fetchone()
    return int(row[0]) if row else 0


def _zscore(series: list[int]) -> float:
    if len(series) < 5:
        return 0.0
    hist, today = series[:-1], series[-1]
    mu = statistics.mean(hist)
    sd = statistics.pstdev(hist)
    return (today - mu) / sd if sd > 0 else 0.0


async def build_daily_signals(
    db_path: Path, universe: list[str], *, date: str | None = None
) -> int:
    """Compute + persist composite signals for the given day. Returns count."""
    day = date or now_utc().strftime("%Y-%m-%d")
    created = iso(now_utc())
    written = 0
    async with aiosqlite.connect(str(db_path)) as db:
        for tk in universe:
            mentions_series = await _daily_mentions(db, tk)
            if not mentions_series:
                continue
            mentions = mentions_series[-1]
            z_attn = _zscore(mentions_series)
            stance, n_msgs = await _latest_stance(db, tk)
            prior_days = await _days_since_prior_mention(db, tk, day)
            novelty = 1.0 if prior_days < 10 and prior_days >= 0 and mentions > 0 \
                and len(mentions_series) <= 3 else 0.0
            disagreement = 1.0 - abs(stance) if n_msgs else 0.0
            direction = 1.0 if stance >= 0 else -1.0
            composite = max(-3.0, min(3.0,
                0.5 * z_attn * direction
                + 0.3 * stance
                + 0.1 * novelty
                - 0.1 * disagreement,
            ))
            comp = {"z_attn": round(z_attn, 3), "stance": round(stance, 3),
                    "novelty": novelty, "disagreement": round(disagreement, 3),
                    "mentions": mentions, "n_msgs": n_msgs}
            await db.execute(
                """
                INSERT INTO equity_signals(
                    date, ticker, composite, z_attn, stance, novelty,
                    disagreement, mentions, components_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(date, ticker) DO UPDATE SET
                    composite=excluded.composite, z_attn=excluded.z_attn,
                    stance=excluded.stance, novelty=excluded.novelty,
                    disagreement=excluded.disagreement, mentions=excluded.mentions,
                    components_json=excluded.components_json
                """,
                (day, tk, composite, z_attn, stance, novelty, disagreement,
                 mentions, json.dumps(comp), created),
            )
            written += 1
        await db.commit()
    return written


async def latest_signals(
    db_path: Path, *, limit: int = 40
) -> list[dict[str, Any]]:
    async with aiosqlite.connect(str(db_path)) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM equity_signals WHERE date = "
            "(SELECT MAX(date) FROM equity_signals) "
            "ORDER BY composite DESC LIMIT ?",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]
