"""Tier-2 Investigator — empirical-priors addendum §5.

Replaces the ad-hoc investigator output shape with a versioned, validated
`Belief` (see `belief_schema.py`). The legacy `polysim.investigator.agent`
module is preserved for backward compat (intel pipeline still uses it);
this module is the new structured-belief path used by the trading loop's
decision gate (§5.4).

§5.3 guards (enforced architecturally, not just documented):
  * No web/network tool access — only the prompt + market context block.
  * No DB mutation (the investigator never writes to wallets_discovery
    or experiments — those tables belong to discovery).
  * No order issuance (reasoning only; the gate decides).
  * No runtime schema modification (Belief is the contract; semver bumps
    require migration).
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from anthropic import AsyncAnthropic

from polysim.agents.belief_schema import (
    SCHEMA_VERSION,
    Belief,
    BeliefCategory,
)
from polysim.investigator.pricing import compute_cost_cents
from polysim.utils.time import iso, now_utc

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-haiku-4-5"
LOGS_DIR = Path("logs/beliefs")


_SYSTEM = (
    "You are PolySim's Tier-2 investigator. You produce structured\n"
    "beliefs about Polymarket markets so a downstream rule engine can\n"
    "decide whether to act. You DO NOT decide whether to trade. You DO\n"
    "NOT do open-ended research. Your job is to read the cohort signal\n"
    "+ market state we hand you and emit a Belief in JSON.\n"
    "\n"
    "Categories: 'event_analysis' (what is the market about, what would\n"
    "make it resolve YES/NO), 'risk_assessment' (resolution criteria\n"
    "ambiguity, oracle issues, time-zone edge cases), 'trading_strategy'\n"
    "(EV vs spread, sizing implications), 'market_structure' (depth,\n"
    "liquidity, expected slippage). Pick the category that BEST fits the\n"
    "single most important point you have to make.\n"
    "\n"
    "Output a single JSON object with EXACTLY these keys:\n"
    "  market_id: string\n"
    "  category: one of the four above\n"
    "  confidence: float in [0,1] — your confidence in your reading\n"
    "  estimated_true_probability: float in [0,1] — your best estimate\n"
    "    of P(market resolves YES)\n"
    "  resolution_risk_score: float in [0,1] — likelihood the resolution\n"
    "    criteria are ambiguous or could be interpreted strictly against\n"
    "    a position. Higher for: UMA optimistic oracle markets with\n"
    "    subjective criteria; geographic specificity (e.g. 'Greater\n"
    "    Beirut'); exact-day deadlines; prior disputed resolutions.\n"
    "  expected_value_per_contract: float — your EV for the *cohort's*\n"
    "    side at the current price. Positive = supports the cohort.\n"
    "  rationale: one sentence — why.\n"
    "  cohort_wallets_involved: list of wallet addresses we told you\n"
    "    about (just echo them back).\n"
    "\n"
    "Output JSON only, no prose before or after."
)


@dataclass(frozen=True)
class InvestigatorContext:
    """The (small) input set we hand to the investigator."""

    market_id: str
    market_question: str
    market_category: str | None
    current_bid_cents: int | None
    current_ask_cents: int | None
    cohort_side: str                          # "YES" | "NO"
    cohort_wallets: list[str]
    cohort_signal_strength: float
    recent_volume_cents_24h: int | None = None
    # One-line public-conversation summary (signals.service.format_signal_note).
    # The pipeline fetches it — the investigator itself still has no network
    # access (§5.3 guard intact); this is just more context in the prompt.
    external_signal_note: str | None = None


@dataclass(frozen=True)
class InvestigatorResult:
    belief: Belief
    raw_response: str
    cost_cents: int
    latency_ms: int
    model: str


def _parse_belief(
    raw_text: str, *, fallback_market_id: str, cohort_wallets: list[str]
) -> Belief | None:
    """Parse the model's JSON response into a validated Belief.

    Returns None on parse/validation failure; the gate treats that as a
    hard reject (no trade). Lenient on common artifacts (markdown fences).
    """
    text = raw_text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.endswith("```"):
            text = text[:-3].rstrip()
        if text.lower().startswith("json"):
            text = text[4:].lstrip()
    try:
        data: Any = json.loads(text)
    except json.JSONDecodeError:
        log.warning("investigator: non-JSON response: %s", text[:200])
        return None
    if not isinstance(data, dict):
        return None
    # Validate via Pydantic — schema_version is set by us, not the model.
    try:
        return Belief(
            market_id=str(data.get("market_id") or fallback_market_id),
            category=_norm_category(data.get("category")),
            confidence=_clamp(data.get("confidence"), 0.0, 1.0),
            estimated_true_probability=_clamp(
                data.get("estimated_true_probability"), 0.0, 1.0,
            ),
            resolution_risk_score=_clamp(
                data.get("resolution_risk_score"), 0.0, 1.0,
            ),
            expected_value_per_contract=float(
                data.get("expected_value_per_contract") or 0.0
            ),
            rationale=str(data.get("rationale") or "")[:280],
            cohort_wallets_involved=list(
                data.get("cohort_wallets_involved") or cohort_wallets
            ),
            schema_version=SCHEMA_VERSION,
            timestamp=datetime.now(UTC),
        )
    except (TypeError, ValueError) as exc:
        log.warning("investigator: belief validation failed: %s", exc)
        return None


def _norm_category(v: Any) -> BeliefCategory:
    s = str(v or "").strip().lower()
    if s in {"event_analysis", "risk_assessment", "trading_strategy", "market_structure"}:
        return s  # type: ignore[return-value]
    return "event_analysis"


def _clamp(v: Any, lo: float, hi: float) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return (lo + hi) / 2
    return max(lo, min(hi, x))


def _build_user_prompt(ctx: InvestigatorContext) -> str:
    bid = ctx.current_bid_cents if ctx.current_bid_cents is not None else "?"
    ask = ctx.current_ask_cents if ctx.current_ask_cents is not None else "?"
    vol = (
        f"${ctx.recent_volume_cents_24h/100:,.0f}"
        if ctx.recent_volume_cents_24h is not None else "?"
    )
    cohort = ", ".join(ctx.cohort_wallets[:8]) or "(none)"
    signal_line = (
        f"PUBLIC CONVERSATION (pre-fetched): {ctx.external_signal_note}\n"
        if ctx.external_signal_note else ""
    )
    return (
        f"MARKET: {ctx.market_id}\n"
        f"QUESTION: {ctx.market_question}\n"
        f"CATEGORY: {ctx.market_category or 'unknown'}\n"
        f"CURRENT BID/ASK (cents): {bid} / {ask}\n"
        f"24h VOLUME: {vol}\n"
        f"COHORT SIDE: {ctx.cohort_side}\n"
        f"COHORT SIGNAL STRENGTH: {ctx.cohort_signal_strength:.3f}\n"
        f"COHORT WALLETS (sample): {cohort}\n"
        f"{signal_line}"
        "\n"
        "Produce the Belief JSON now. No tool use, no web search, no prose."
    )


async def investigate(
    ctx: InvestigatorContext,
    *,
    client: AsyncAnthropic,
    model: str = DEFAULT_MODEL,
    max_tokens: int = 600,
    persist_log: bool = True,
) -> InvestigatorResult | None:
    """Single LLM call → Belief. Logs JSONL to `logs/beliefs/<date>.jsonl`."""
    started = time.monotonic()
    resp = await client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=_SYSTEM,
        messages=[{"role": "user", "content": _build_user_prompt(ctx)}],
    )
    latency_ms = int((time.monotonic() - started) * 1000)
    raw = "".join(
        str(getattr(block, "text", ""))
        for block in resp.content
        if getattr(block, "type", None) == "text"
    )
    belief = _parse_belief(
        raw, fallback_market_id=ctx.market_id,
        cohort_wallets=ctx.cohort_wallets,
    )
    if belief is None:
        return None

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
    if persist_log:
        await _append_belief_log(belief)
    return InvestigatorResult(
        belief=belief, raw_response=raw,
        cost_cents=cost, latency_ms=latency_ms, model=model,
    )


async def _append_belief_log(belief: Belief) -> None:
    """Append one Belief as a JSON line to logs/beliefs/<YYYY-MM-DD>.jsonl."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    today = belief.timestamp.astimezone(UTC).date().isoformat()
    target = LOGS_DIR / f"{today}.jsonl"
    line = belief.model_dump_json() + "\n"
    # Pure sync I/O — beliefs are tiny + we don't want async-subprocess churn.
    with target.open("a", encoding="utf-8") as f:
        f.write(line)
    _ = iso(now_utc())  # keep iso import referenced (will be used by planner)


__all__ = [
    "DEFAULT_MODEL",
    "InvestigatorContext",
    "InvestigatorResult",
    "investigate",
]
