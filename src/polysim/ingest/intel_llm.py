"""LLM-driven sentiment + market-hint extraction for intel messages.

Consumes the raw text of each `intel_messages` row and asks Claude Haiku
to pull out:
  * is_market_relevant   — whether this is about a tradeable event
  * market_hint          — human phrase (e.g. "US x Iran ceasefire by Apr 15")
  * direction            — "YES" / "NO" / null
  * conviction           — 0..1 confidence the poster seems to have
  * sentiment            — bullish / bearish / neutral / mixed
  * tags                 — short tokens ("insider", "coordination", "thread")
  * summary              — one-sentence paraphrase

Fuzzy-matches `market_hint` against our `markets.question` column so each
interpretation can be tied to a concrete market (nullable when no match
crosses the threshold).

Uses the already-configured Anthropic client wiring + pricing.
"""

from __future__ import annotations

import difflib
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from anthropic import AsyncAnthropic

from polysim.db import dao
from polysim.investigator.pricing import compute_cost_cents
from polysim.utils.time import iso, now_utc

log = logging.getLogger(__name__)

PROMPT_VERSION = "v2-entities"
DEFAULT_MODEL = "claude-haiku-4-5"

_SYSTEM = (
    "You parse forecast/commentary/news posts into structured JSON for a "
    "prediction-market insider-detection system. Posts come from many "
    "domains — AI launches, architecture/engineering/construction (AEC), "
    "creator economy (YouTubers, MrBeast), geopolitics, pop culture, box "
    "office, macro + housing, tech M&A, crypto launches, gov/defense, "
    "sports (NFL/NBA/MLB), elections — NOT just crypto.\n"
    "\n"
    "Return a single JSON object with EXACTLY these keys:\n"
    "  is_market_relevant: bool — does the post forecast, report on, or\n"
    "    reference a specific future-or-recent outcome that a prediction\n"
    "    market would trade on? (opening-weekend grosses, game winners,\n"
    "    election results, model launches, building permits, token\n"
    "    launches, M&A announcements, defense procurement all count.)\n"
    "  market_hint: string | null — short phrase naming the specific\n"
    "    event/outcome. Examples: 'Sims Movie opening weekend', 'Bills\n"
    "    beat Chiefs week 17', 'Trump wins 2028 primary', 'Anthropic\n"
    "    ships Claude 5 by Q3', 'Figma acquires Adobe', 'NBA Finals MVP\n"
    "    Jokic'.\n"
    "  entities: list of proper-noun strings central to the post. Include\n"
    "    movie titles ('Super Mario Galaxy Movie'), team/league names\n"
    "    ('Bills', 'NFL'), politicians ('Trump', 'Milei'), companies\n"
    "    ('OpenAI', 'Figma', 'Autodesk'), tokens ('PUMP', 'ENA'),\n"
    "    creators ('MrBeast'), events ('WWDC 2026'). Use canonical display\n"
    "    form. Short (<5 words each); max 6 entities. Entities drive\n"
    "    market matching — always extract them even when the post itself\n"
    "    isn't making a prediction.\n"
    "  direction: 'YES' | 'NO' | null — the direction of the outcome the\n"
    "    poster expects or reports as likely. YES = event/threshold\n"
    "    clears; NO = doesn't. null when neutral/ambiguous.\n"
    "  conviction: float in [0,1] — 0.9 for 'lock / insider info', 0.6\n"
    "    for 'confident forecast with reasoning', 0.3 for 'guess', 0 for\n"
    "    'meta chat'.\n"
    "  sentiment: 'bullish' | 'bearish' | 'neutral' | 'mixed' — toward\n"
    "    the entities / outcome described.\n"
    "  tags: short tokens from {insider, coordination, whale, thread,\n"
    "    news, analysis, forecast, shill, airdrop, offtopic,\n"
    "    box_office, sports, election, ai, creator, ma, macro,\n"
    "    geopolitics, tech, gov_defense}\n"
    "  summary: one-sentence paraphrase, ≤ 140 chars\n"
    "\n"
    "Rules:\n"
    "  - Post-facto news ('Movie X grossed $20M last weekend') IS\n"
    "    market-relevant — entity matches feed other active predictions.\n"
    "  - Subreddit posts often debate upcoming films/games — these are\n"
    "    prediction-relevant even when framed as opinion.\n"
    "  - Whale-transfer alerts: if a large stablecoin move hints at\n"
    "    position-taking on a named asset, it's relevant; pure custody\n"
    "    moves are offtopic.\n"
    "  - If the post is purely personal/meme/sponsorship/sports chatter\n"
    "    with no named entity, mark offtopic.\n"
    "  - Output JSON ONLY, no prose before or after."
)


@dataclass(frozen=True)
class InterpretedIntel:
    is_market_relevant: bool
    market_hint: str | None
    direction: str | None              # "YES" | "NO" | None
    conviction: float                  # 0..1
    sentiment: str                     # bullish | bearish | neutral | mixed
    tags: list[str]
    summary: str
    entities: list[str]                # proper nouns for entity-match


@dataclass(frozen=True)
class InterpretationUsage:
    model: str
    input_tokens: int
    output_tokens: int
    cost_cents: int
    latency_ms: int


def _parse_response(raw_text: str) -> InterpretedIntel:
    """Parse the model's JSON response. Lenient — strips common artifacts."""
    text = raw_text.strip()
    # Strip markdown code fences if present.
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.endswith("```"):
            text = text[: -3].rstrip()
        # Strip a leading "json" language tag.
        if text.lower().startswith("json"):
            text = text[4:].lstrip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Fall back to "safe defaults" so the pipeline never dies on one
        # weird response.
        log.warning("intel interp: non-JSON response: %s", text[:200])
        return InterpretedIntel(
            is_market_relevant=False,
            market_hint=None,
            direction=None,
            conviction=0.0,
            sentiment="neutral",
            tags=[],
            summary="(unparseable)",
            entities=[],
        )
    raw_entities = data.get("entities") or []
    entities = [
        str(e).strip()
        for e in raw_entities
        if isinstance(e, str) and e.strip()
    ][:8]  # cap, in case model over-produces
    return InterpretedIntel(
        is_market_relevant=bool(data.get("is_market_relevant")),
        market_hint=(data.get("market_hint") or None),
        direction=_norm_direction(data.get("direction")),
        conviction=_clamp(data.get("conviction"), 0.0, 1.0, default=0.5),
        sentiment=str(data.get("sentiment") or "neutral").lower(),
        tags=[str(t).lower() for t in (data.get("tags") or [])],
        summary=str(data.get("summary") or "").strip()[:200],
        entities=entities,
    )


def _norm_direction(v: Any) -> str | None:
    if isinstance(v, str):
        u = v.strip().upper()
        if u in {"YES", "NO"}:
            return u
    return None


def _clamp(v: Any, lo: float, hi: float, *, default: float) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, x))


async def interpret(
    text: str,
    *,
    client: AsyncAnthropic,
    model: str = DEFAULT_MODEL,
    max_tokens: int = 300,
) -> tuple[InterpretedIntel, InterpretationUsage]:
    """Call Claude Haiku with `text`. Returns (parsed, usage)."""
    started = time.monotonic()
    resp = await client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=_SYSTEM,
        messages=[{"role": "user", "content": text[:2_000]}],
    )
    latency_ms = int((time.monotonic() - started) * 1000)
    # The content list is a union of block types; only TextBlock has .text,
    # so grab it defensively via getattr.
    raw = "".join(
        str(getattr(block, "text", ""))
        for block in resp.content
        if getattr(block, "type", None) == "text"
    )
    parsed = _parse_response(raw)

    usage = resp.usage
    cost = compute_cost_cents(
        model=model,
        input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
        output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
        cache_creation_tokens=int(
            getattr(usage, "cache_creation_input_tokens", 0) or 0
        ),
        cache_read_tokens=int(getattr(usage, "cache_read_input_tokens", 0) or 0),
    )
    return parsed, InterpretationUsage(
        model=model,
        input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
        output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
        cost_cents=cost,
        latency_ms=latency_ms,
    )


# ── market matcher ───────────────────────────────────────


_STOPWORDS = frozenset({
    "the", "a", "an", "of", "to", "in", "on", "for", "by", "at", "is", "be",
    "will", "would", "does", "do", "has", "have", "as", "and", "or", "but",
    "with", "this", "that", "which", "what", "when", "where", "how", "why",
    "market", "markets", "direction", "price", "post", "buy", "sell",
    "yes", "no",
})


def _tokenize(s: str) -> set[str]:
    """Lowercase, alphanumeric-only tokens, stopwords stripped."""
    out: set[str] = set()
    cur = []
    for ch in s.lower():
        if ch.isalnum():
            cur.append(ch)
        else:
            if cur:
                tok = "".join(cur)
                if tok and tok not in _STOPWORDS and len(tok) > 1:
                    out.add(tok)
                cur = []
    if cur:
        tok = "".join(cur)
        if tok and tok not in _STOPWORDS and len(tok) > 1:
            out.add(tok)
    return out


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


async def match_market(
    db_path: Path,
    *,
    market_hint: str,
    min_score: float = 0.30,                   # lowered from 0.45
    candidate_limit: int = 400,                # wider after historical backfill
    prefer_recent_days: int = 365,
) -> tuple[str | None, float]:
    """Fuzzy-match a free-text hint to one of our markets.

    Scoring = 0.35·sequence_sim + 0.45·jaccard_tokens + 0.20·keyword_overlap,
    with a small recency tie-breaker boost (+0.05 when the market resolved
    within `prefer_recent_days`).

    Returns (market_id, score). Only returns a match when score ≥ min_score.
    """
    if not market_hint:
        return None, 0.0
    from datetime import UTC, datetime, timedelta

    import aiosqlite

    if not db_path.exists():
        return None, 0.0
    hint = market_hint.lower()
    hint_tokens = _tokenize(hint)
    if not hint_tokens:
        return None, 0.0
    recent_cutoff = (datetime.now(UTC) - timedelta(days=prefer_recent_days)).isoformat()
    try:
        async with aiosqlite.connect(str(db_path)) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT id, question, slug, resolved_at, resolves_at
                FROM markets
                WHERE question IS NOT NULL
                  AND (resolves_at IS NULL OR resolves_at >= ?)
                ORDER BY COALESCE(daily_volume_usd_cents, 0) DESC LIMIT ?
                """,
                (recent_cutoff, candidate_limit),
            ) as cur:
                rows = list(await cur.fetchall())
    except aiosqlite.OperationalError:
        return None, 0.0

    best_id: str | None = None
    best_score = 0.0
    for r in rows:
        q = str(r["question"] or "").lower()
        if not q:
            continue
        q_tokens = _tokenize(q)
        if not q_tokens:
            continue
        sim = difflib.SequenceMatcher(None, hint, q).ratio()
        jac = _jaccard(hint_tokens, q_tokens)
        # Plain word-overlap (hint_tokens recall).
        overlap = len(hint_tokens & q_tokens) / max(1, len(hint_tokens))
        score = 0.35 * sim + 0.45 * jac + 0.20 * overlap
        # Tiny recency boost so tied scores prefer recent markets.
        if r["resolved_at"] is not None:
            score += 0.02
        if score > best_score:
            best_score = score
            best_id = str(r["id"])
    if best_score < min_score:
        return None, best_score
    return best_id, best_score


_MATCH_SYSTEM = (
    "You match a free-text post hint to a concrete Polymarket market from "
    "a numbered shortlist.\n"
    "Return a single JSON object with keys:\n"
    '  market_id: string | null — the `id` of the best match, or null if none fit\n'
    "  confidence: float in [0,1] — how confident you are\n"
    "  reasoning: string — one short sentence\n"
    "Rules:\n"
    "- Only pick a market if the hint is clearly about the SAME event.\n"
    "- If the hint is vague (e.g. 'Bitcoin price direction') and no market\n"
    "  is a specific fit, return market_id=null.\n"
    "- Prefer markets whose question wording overlaps the hint.\n"
    "- Output JSON only, no prose."
)


async def match_market_llm(
    db_path: Path,
    *,
    market_hint: str,
    client: AsyncAnthropic,
    model: str = DEFAULT_MODEL,
    top_k: int = 20,
    min_confidence: float = 0.5,
    entities: list[str] | None = None,
) -> tuple[str | None, float, InterpretationUsage]:
    """LLM-backed market matcher.

    1. Shortlist top-K candidate markets:
       * If `entities` supplied, SQL LIKE against them (handles name-only
         mentions like "Sims Movie" → "Sims Movie opening weekend").
       * Else fall back to hint-similarity shortlist.
    2. Ask Claude Haiku to pick the best fit (or null).
    3. Return (market_id, confidence, usage).
    """
    import time as _time

    candidates: list[dict[str, Any]] = []
    if entities:
        candidates = await candidate_markets_for_entities(
            db_path, entities=entities, top_k=top_k,
        )
    if not candidates:
        candidates = await candidate_markets_for_hint(
            db_path, market_hint=market_hint, top_k=top_k,
        )
    zero_usage = InterpretationUsage(
        model=model, input_tokens=0, output_tokens=0, cost_cents=0, latency_ms=0,
    )
    if not candidates:
        return None, 0.0, zero_usage

    lines = [f"HINT: {market_hint}", "", "CANDIDATES:"]
    for i, c in enumerate(candidates, 1):
        q = str(c.get("question") or "")[:180]
        resolved = (
            f" [resolved {c.get('resolved_outcome')}]"
            if c.get("resolved_outcome") else ""
        )
        lines.append(f"{i}. id={c['id']}  {q}{resolved}")
    prompt_text = "\n".join(lines)

    started = _time.monotonic()
    resp = await client.messages.create(
        model=model,
        max_tokens=200,
        system=_MATCH_SYSTEM,
        messages=[{"role": "user", "content": prompt_text}],
    )
    latency_ms = int((_time.monotonic() - started) * 1000)
    raw = "".join(
        str(getattr(block, "text", ""))
        for block in resp.content
        if getattr(block, "type", None) == "text"
    ).strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1]
        if raw.endswith("```"):
            raw = raw[:-3].rstrip()
        if raw.lower().startswith("json"):
            raw = raw[4:].lstrip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = {}

    usage = resp.usage
    cost = compute_cost_cents(
        model=model,
        input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
        output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
        cache_creation_tokens=int(
            getattr(usage, "cache_creation_input_tokens", 0) or 0
        ),
        cache_read_tokens=int(getattr(usage, "cache_read_input_tokens", 0) or 0),
    )
    usage_out = InterpretationUsage(
        model=model,
        input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
        output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
        cost_cents=cost,
        latency_ms=latency_ms,
    )

    mid = data.get("market_id")
    conf = _clamp(data.get("confidence"), 0.0, 1.0, default=0.0)
    if not mid or not isinstance(mid, str) or conf < min_confidence:
        return None, conf, usage_out
    # Validate it was actually one of our shortlisted ids (anti-hallucination).
    valid = {str(c["id"]) for c in candidates}
    if mid not in valid:
        log.warning("LLM matcher returned invalid id %s (not in shortlist)", mid)
        return None, conf, usage_out
    return mid, conf, usage_out


async def candidate_markets_for_entities(
    db_path: Path,
    *,
    entities: list[str],
    top_k: int = 20,
    per_entity_limit: int = 100,
) -> list[dict[str, Any]]:
    """Shortlist markets whose question contains any of the entity strings.

    Each entity is matched via SQL LIKE '%token%' for every ≥ 4-char word
    in the entity. A market scores proportional to the number of entity
    tokens it matches (intersection). Returns the top-K overall.
    """
    import aiosqlite as _a

    if not entities or not db_path.exists():
        return []
    # Build tokens from entities — keep all useful-length words (≥4 chars,
    # strip stopwords).
    tokens: list[str] = []
    seen: set[str] = set()
    for e in entities:
        for word in e.split():
            w = "".join(ch for ch in word.lower() if ch.isalnum())
            if (
                len(w) >= 4
                and w not in _STOPWORDS
                and w not in seen
            ):
                seen.add(w)
                tokens.append(w)
    if not tokens:
        return []

    # For each token, pull up to `per_entity_limit` candidate markets.
    market_scores: dict[str, tuple[float, dict[str, Any]]] = {}
    try:
        async with _a.connect(str(db_path)) as db:
            db.row_factory = _a.Row
            for tok in tokens:
                like = f"%{tok}%"
                async with db.execute(
                    """
                    SELECT id, question, slug, resolved_outcome, resolved_at,
                           category, daily_volume_usd_cents
                    FROM markets
                    WHERE question LIKE ?
                    ORDER BY COALESCE(daily_volume_usd_cents, 0) DESC LIMIT ?
                    """,
                    (like, per_entity_limit),
                ) as cur:
                    async for r in cur:
                        mid = str(r["id"])
                        prev = market_scores.get(mid)
                        prev_score = prev[0] if prev else 0.0
                        market_scores[mid] = (prev_score + 1.0, dict(r))
    except _a.OperationalError:
        return []
    ranked = sorted(
        market_scores.values(), key=lambda t: (-t[0], 0),
    )
    return [m for _s, m in ranked[:top_k]]


async def candidate_markets_for_hint(
    db_path: Path,
    *,
    market_hint: str,
    top_k: int = 20,
    candidate_limit: int = 400,
) -> list[dict[str, Any]]:
    """Return the top-K candidate markets for a hint, ranked by the same
    scoring as `match_market` — used by the LLM fallback to shortlist."""
    import aiosqlite

    if not market_hint or not db_path.exists():
        return []
    hint_tokens = _tokenize(market_hint)
    if not hint_tokens:
        return []
    try:
        async with aiosqlite.connect(str(db_path)) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT id, question, slug, resolved_outcome, resolved_at, category "
                "FROM markets WHERE question IS NOT NULL "
                "ORDER BY COALESCE(daily_volume_usd_cents, 0) DESC LIMIT ?",
                (candidate_limit,),
            ) as cur:
                rows = list(await cur.fetchall())
    except aiosqlite.OperationalError:
        return []

    scored: list[tuple[float, dict[str, Any]]] = []
    hint = market_hint.lower()
    for r in rows:
        q = str(r["question"] or "").lower()
        if not q:
            continue
        q_tokens = _tokenize(q)
        if not q_tokens:
            continue
        sim = difflib.SequenceMatcher(None, hint, q).ratio()
        jac = _jaccard(hint_tokens, q_tokens)
        overlap = len(hint_tokens & q_tokens) / max(1, len(hint_tokens))
        score = 0.35 * sim + 0.45 * jac + 0.20 * overlap
        scored.append((score, dict(r)))
    scored.sort(key=lambda t: -t[0])
    return [d for _, d in scored[:top_k]]


# ── DB helpers ───────────────────────────────────────────


async def save_interpretation(
    db_path: Path,
    *,
    intel_message_id: int,
    interp: InterpretedIntel,
    matched_market_id: str | None,
    match_score: float,
    usage: InterpretationUsage,
    prompt_version: str = PROMPT_VERSION,
) -> int:
    import aiosqlite

    async with aiosqlite.connect(str(db_path)) as db:
        cur = await db.execute(
            """
            INSERT INTO intel_interpretations(
                intel_message_id, model, prompt_version,
                is_market_relevant, sentiment, direction, conviction,
                market_hint, matched_market_id, match_score,
                tags_json, summary, entities_json,
                cost_cents, input_tokens, output_tokens, latency_ms,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                intel_message_id, usage.model, prompt_version,
                1 if interp.is_market_relevant else 0,
                interp.sentiment, interp.direction, interp.conviction,
                interp.market_hint, matched_market_id, match_score,
                json.dumps(interp.tags), interp.summary,
                json.dumps(interp.entities),
                usage.cost_cents, usage.input_tokens, usage.output_tokens,
                usage.latency_ms, iso(now_utc()),
            ),
        )
        new_id = int(cur.lastrowid or 0)
        await cur.close()
        await db.commit()
    return new_id


async def list_interpretations(
    db_path: Path, *, limit: int = 50, source: str | None = None
) -> list[dict[str, Any]]:
    import aiosqlite

    if not db_path.exists():
        return []
    query = (
        "SELECT i.*, m.source, m.posted_at, m.external_id, m.text "
        "FROM intel_interpretations i "
        "JOIN intel_messages m ON m.id = i.intel_message_id"
    )
    args: list[Any] = []
    if source is not None:
        query += " WHERE m.source = ?"
        args.append(source)
    query += " ORDER BY m.posted_at DESC LIMIT ?"
    args.append(limit)
    try:
        async with aiosqlite.connect(str(db_path)) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(query, args) as cur:
                rows = await cur.fetchall()
    except aiosqlite.OperationalError:
        return []
    _ = dao  # keep import for layering audit
    return [dict(r) for r in rows]


async def already_interpreted_ids(
    db_path: Path, *, prompt_version: str = PROMPT_VERSION
) -> set[int]:
    """Return the set of intel_message_id values with an interpretation at
    this prompt_version — used to avoid re-running LLM calls."""
    import aiosqlite

    if not db_path.exists():
        return set()
    try:
        async with aiosqlite.connect(str(db_path)) as db, db.execute(
            "SELECT DISTINCT intel_message_id FROM intel_interpretations "
            "WHERE prompt_version = ?",
            (prompt_version,),
        ) as cur:
            rows = await cur.fetchall()
    except aiosqlite.OperationalError:
        return set()
    return {int(r[0]) for r in rows if r[0] is not None}


__all__ = [
    "DEFAULT_MODEL",
    "PROMPT_VERSION",
    "InterpretationUsage",
    "InterpretedIntel",
    "already_interpreted_ids",
    "interpret",
    "list_interpretations",
    "match_market",
    "save_interpretation",
]
