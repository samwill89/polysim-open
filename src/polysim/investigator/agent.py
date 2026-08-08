"""Claude-based investigator agent — spec §7.5.

Phase 2 wired the call path. Phase 3 adds:
  - Prompt caching: SYSTEM_PROMPT + wallet-history block marked
    `cache_control: ephemeral`, so repeat queries on the same wallet
    (same trade_count) hit the Anthropic prompt cache and save cost.
  - Cost accounting: each call returns a `UsageStats` the orchestrator
    persists to `flag_costs` (G7).
  - Orchestrator `investigate_flag`: fetches DB inputs for a flag, runs
    the agent, writes verdict + cost row, optionally toggles `acted_on`.

Hard constraints:
  §14 #6 — investigator is a FILTER, never a scorer. It can only change
           `investigator_verdict`, `investigator_reasoning`, and
           `acted_on`.  Composite scores are immutable here.
  G14     — user-controlled strings sanitized before prompt injection.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, date
from pathlib import Path
from typing import Any

from anthropic import AsyncAnthropic

from polysim.db import dao
from polysim.investigator.pricing import compute_cost_cents
from polysim.investigator.prompts import (
    MAX_TRADE_HISTORY,
    SYSTEM_PROMPT,
    build_user_prompt,
    sanitize,
)
from polysim.models import (
    Flag,
    InvestigatorOutcome,
    InvestigatorVerdict,
    Market,
    TradeEvent,
    WalletProfile,
)
from polysim.utils.time import now_utc

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class UsageStats:
    """Per-call token + cost snapshot.  Persisted to `flag_costs`."""

    model: str
    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int
    cost_cents: int
    latency_ms: int


@dataclass
class DailyCallCounter:
    day: date = field(default_factory=lambda: now_utc().date())
    count: int = 0

    def bump(self) -> None:
        today = now_utc().date()
        if today != self.day:
            self.day = today
            self.count = 0
        self.count += 1

    def remaining(self, cap: int) -> int:
        today = now_utc().date()
        if today != self.day:
            return cap
        return max(0, cap - self.count)


class Investigator:
    def __init__(
        self,
        *,
        api_key: str,
        model: str = "claude-opus-4-7",
        triage_model: str = "claude-haiku-4-5",
        triage_threshold: float = 7.0,
        max_calls_per_day: int = 100,
        min_verdict_confidence_to_act: float = 0.6,
        max_tokens: int = 1024,
        client: AsyncAnthropic | None = None,
    ) -> None:
        if not api_key and client is None:
            log.warning("Investigator instantiated with empty API key")
        self._client = client or AsyncAnthropic(api_key=api_key or "missing")
        self.model = model
        self.triage_model = triage_model
        self.triage_threshold = triage_threshold
        self.max_calls_per_day = max_calls_per_day
        self.min_verdict_confidence_to_act = min_verdict_confidence_to_act
        self.max_tokens = max_tokens
        self._calls = DailyCallCounter()

    def remaining_calls(self) -> int:
        return self._calls.remaining(self.max_calls_per_day)

    def should_act(self, outcome: InvestigatorOutcome) -> bool:
        """§7.5 — only act on INFORMED with confidence >= threshold."""
        return (
            outcome.verdict == "INFORMED"
            and outcome.confidence >= self.min_verdict_confidence_to_act
        )

    def _pick_model(self, composite_score: float) -> str:
        return (
            self.model
            if composite_score >= self.triage_threshold
            else self.triage_model
        )

    async def investigate(
        self,
        flag: Flag,
        wallet_profile: WalletProfile,
        wallet_trade_history: list[TradeEvent],
        market: Market,
        *,
        wallet_display_name: str | None = None,
    ) -> tuple[InvestigatorOutcome | None, UsageStats | None]:
        """Run the investigator. Returns (outcome, usage) or (None, None)
        when the daily cap is hit.

        On API or parse failure the outcome is a defensive UNCLEAR; the
        caller persists it so the flag's status is never left ambiguous.
        """
        if self.remaining_calls() <= 0:
            log.info(
                "investigator daily cap reached (%d/%d); falling back",
                self._calls.count,
                self.max_calls_per_day,
            )
            return None, None

        model = self._pick_model(flag.composite_score or 0.0)

        history = wallet_trade_history[-MAX_TRADE_HISTORY:]
        cached_block = _render_cached_block(
            wallet_address=flag.wallet_address,
            wallet_display_name=wallet_display_name,
            wallet_profile=wallet_profile,
            trade_history=history,
        )
        trigger_block = _render_trigger_block(
            flag=flag,
            triggering_trade=_find_trade(wallet_trade_history, flag.trade_id),
            market=market,
        )

        self._calls.bump()
        start = time.monotonic()
        try:
            response = await self._client.messages.create(
                model=model,
                max_tokens=self.max_tokens,
                system=[
                    {
                        "type": "text",
                        "text": SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    },
                ],
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": cached_block,
                                "cache_control": {"type": "ephemeral"},
                            },
                            {"type": "text", "text": trigger_block},
                        ],
                    },
                ],
            )
        except Exception as exc:
            latency_ms = int((time.monotonic() - start) * 1000)
            log.warning("investigator API call failed after %dms: %s", latency_ms, exc)
            return _default_unclear("API error"), None

        latency_ms = int((time.monotonic() - start) * 1000)
        usage = _extract_usage(response, model=model, latency_ms=latency_ms)

        text = _extract_text(response)
        outcome = _parse_verdict_json(text)
        if outcome is None:
            return _default_unclear("Malformed JSON response"), usage
        return outcome, usage


# ── orchestrator ─────────────────────────────────────────────


async def investigate_flag(
    db_path: Path,
    investigator: Investigator,
    flag_id: int,
    *,
    set_acted_on: bool = True,
) -> InvestigatorOutcome | None:
    """Full flow for one flag: load inputs -> run -> persist verdict + cost.

    Returns the verdict (even a defensive UNCLEAR), or None when the daily
    cap is hit or inputs are missing.
    """
    row = await dao.get_flag(db_path, flag_id)
    if row is None:
        log.warning("investigate_flag: flag #%d not found", flag_id)
        return None

    wallet_address = str(row["wallet_address"]).lower()
    market_id = str(row["market_id"])

    profile = await dao.get_latest_profile(db_path, wallet_address)
    if profile is None:
        log.warning("investigate_flag: no profile for wallet %s", wallet_address)
        return None
    wallet = await dao.get_wallet(db_path, wallet_address)
    market = await dao.get_market(db_path, market_id)
    if market is None:
        log.warning("investigate_flag: market %s missing", market_id)
        return None
    history = await dao.get_trades_by_wallet(db_path, wallet_address, limit=200)

    flag = _flag_from_row(row)
    outcome, usage = await investigator.investigate(
        flag,
        profile,
        history,
        market,
        wallet_display_name=(wallet.display_name if wallet is not None else None),
    )
    if outcome is None:
        return None  # cap-hit fallback; caller leaves verdict NULL

    acted_on: bool | None = None
    if set_acted_on:
        acted_on = investigator.should_act(outcome)

    await dao.update_flag_investigator(
        db_path,
        flag_id,
        verdict=outcome.verdict,
        reasoning=_format_reasoning(outcome),
        acted_on=acted_on,
    )
    if usage is not None:
        await dao.record_flag_cost(
            db_path,
            flag_id,
            model=usage.model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cached_tokens=usage.cache_read_tokens + usage.cache_creation_tokens,
            cost_cents=usage.cost_cents,
            latency_ms=usage.latency_ms,
        )
    return outcome


# ── prompt block rendering ───────────────────────────────────


def _render_cached_block(
    *,
    wallet_address: str,
    wallet_display_name: str | None,
    wallet_profile: WalletProfile,
    trade_history: list[TradeEvent],
) -> str:
    """Cacheable block: wallet identity + profile + last-N trades.

    Key insight: this block is a pure function of (wallet_address,
    len(trade_history)) — so the second call about the same wallet
    hits the Anthropic prompt cache provided trade_count is unchanged.
    """
    payload = {
        "wallet": {
            "address": wallet_address,
            "display_name": sanitize(wallet_display_name, max_len=64),
            "profile": _profile_to_dict(wallet_profile),
        },
        "trade_history_last_N": [_trade_to_dict(t) for t in trade_history if t is not None],
    }
    return (
        "--- wallet context (cacheable) ---\n"
        + json.dumps(payload, default=str, indent=2)
    )


def _render_trigger_block(
    *,
    flag: Flag,
    triggering_trade: TradeEvent | None,
    market: Market,
) -> str:
    """Per-flag block — NOT cached.  Changes on every call."""
    payload = {
        "market": {
            "id": market.id,
            "question": sanitize(market.question, max_len=500),
            "category": market.category,
            "daily_volume_cents": market.daily_volume_usd_cents,
        },
        "triggering_trade": _trade_to_dict(triggering_trade),
        "composite": {
            "score": flag.composite_score or flag.raw_score,
            "components": flag.components,
        },
    }
    return (
        "--- triggering flag context ---\n"
        + json.dumps(payload, default=str, indent=2)
        + "\n\nReturn the JSON verdict now."
    )


# Kept for backwards-compat tests — callers can still call build_user_prompt
# directly for the simpler, non-cached path.
_ = build_user_prompt


# ── helpers ──────────────────────────────────────────────────


def _default_unclear(reason: str) -> InvestigatorOutcome:
    return InvestigatorOutcome(
        verdict="UNCLEAR",
        confidence=0.0,
        reasoning=f"Investigator unavailable: {reason}",
    )


def _extract_text(response: Any) -> str:
    content = getattr(response, "content", None)
    if not content:
        return ""
    parts: list[str] = []
    for block in content:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts).strip()


def _extract_usage(response: Any, *, model: str, latency_ms: int) -> UsageStats:
    usage = getattr(response, "usage", None)
    input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
    output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
    cache_creation = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
    cache_read = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
    cost = compute_cost_cents(
        model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_tokens=cache_creation,
        cache_read_tokens=cache_read,
    )
    return UsageStats(
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_tokens=cache_creation,
        cache_read_tokens=cache_read,
        cost_cents=cost,
        latency_ms=latency_ms,
    )


def _parse_verdict_json(text: str) -> InvestigatorOutcome | None:
    if not text:
        return None
    t = text.strip()
    if t.startswith("```"):
        lines = [ln for ln in t.splitlines() if not ln.startswith("```")]
        t = "\n".join(lines).strip()
    try:
        raw = json.loads(t)
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, dict):
        return None

    verdict_raw = raw.get("verdict")
    if not isinstance(verdict_raw, str) or verdict_raw.upper() not in {
        "INFORMED",
        "LUCKY",
        "UNCLEAR",
    }:
        return None
    verdict: InvestigatorVerdict = verdict_raw.upper()  # type: ignore[assignment]

    confidence_raw = raw.get("confidence", 0.0)
    try:
        confidence = float(confidence_raw)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    reasoning_raw = raw.get("reasoning")
    reasoning = str(reasoning_raw) if isinstance(reasoning_raw, str) else ""

    def _string_list(v: Any) -> list[str]:
        if isinstance(v, list):
            return [str(x) for x in v if isinstance(x, (str, int, float))]
        return []

    return InvestigatorOutcome(
        verdict=verdict,
        confidence=confidence,
        reasoning=reasoning,
        red_flags=_string_list(raw.get("red_flags")),
        green_flags=_string_list(raw.get("green_flags")),
    )


def _find_trade(history: list[TradeEvent], trade_id: str | None) -> TradeEvent | None:
    if trade_id is None:
        return None
    return next((t for t in history if t.id == trade_id), None)


def _trade_to_dict(trade: TradeEvent | None) -> dict[str, Any] | None:
    if trade is None:
        return None
    return {
        "id": trade.id,
        "market_id": trade.market_id,
        "side": trade.side,
        "outcome": trade.outcome,
        "size_shares": trade.size_shares,
        "price_cents": trade.price_cents,
        "timestamp": trade.timestamp.astimezone(UTC).isoformat(),
    }


def _profile_to_dict(profile: WalletProfile) -> dict[str, Any]:
    return {
        "total_markets": profile.total_markets,
        "resolved_markets": profile.resolved_markets,
        "wins": profile.wins,
        "losses": profile.losses,
        "win_rate": profile.win_rate,
        "total_pnl_cents": profile.total_pnl_cents,
        "categories": profile.categories,
        "category_exclusivity": profile.category_exclusivity,
        "avg_entry_to_resolution_hours": profile.avg_entry_to_resolution_hours,
    }


def _format_reasoning(outcome: InvestigatorOutcome) -> str:
    """Serialize reasoning + red/green flags for the flags.investigator_reasoning column."""
    parts = [outcome.reasoning or "(no reasoning)"]
    if outcome.red_flags:
        parts.append("RED FLAGS:\n  - " + "\n  - ".join(outcome.red_flags))
    if outcome.green_flags:
        parts.append("GREEN FLAGS:\n  - " + "\n  - ".join(outcome.green_flags))
    return "\n\n".join(parts)


def _flag_from_row(row: dict[str, Any]) -> Flag:
    """Rehydrate a Flag pydantic model from a dao.get_flag(...) row."""
    components_raw = row.get("components_json")
    components: dict[str, Any] = {}
    if components_raw:
        try:
            parsed = json.loads(components_raw)
            if isinstance(parsed, dict):
                components = parsed
        except json.JSONDecodeError:
            components = {}
    from polysim.utils.time import parse_iso

    return Flag(
        id=int(row["id"]),
        wallet_address=str(row["wallet_address"]),
        market_id=str(row["market_id"]),
        trade_id=row.get("trade_id"),
        detector_name=str(row["detector_name"]),
        raw_score=float(row["raw_score"]),
        composite_score=(
            float(row["composite_score"])
            if row.get("composite_score") is not None
            else None
        ),
        components=components,
        investigator_verdict=row.get("investigator_verdict"),
        investigator_reasoning=row.get("investigator_reasoning"),
        created_at=parse_iso(str(row["created_at"])),
        acted_on=bool(row.get("acted_on")),
    )
