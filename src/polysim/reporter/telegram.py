"""Telegram alerts — spec §7.8 / build plan §6.3.

Four message types matching demo panel #4:
  * flag raised   (composite score, investigator verdict, paper-position summary)
  * position opened
  * position resolved (win or loss)
  * daily summary (P&L by run + top/worst category + API usage + kill-switch state)

`AlertSink` is the interface consumers depend on. `NullSink` satisfies it
without sending anything, so the CLI / pipeline works when Telegram is
unconfigured. `TelegramSink` wraps python-telegram-bot with MarkdownV2
formatting and token hygiene (§7.4 — token never logged).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Protocol

log = logging.getLogger(__name__)


# ── Message bodies ───────────────────────────────────────


@dataclass(frozen=True)
class FlagAlert:
    flag_id: int
    composite_score: float
    verdict: str | None                # INFORMED / LUCKY / UNCLEAR / None
    verdict_confidence: float | None
    market_question: str
    wallet_address: str
    trade_side: str                    # BUY / SELL
    trade_outcome: str                 # YES / NO
    trade_size_shares: int
    trade_price_cents: int
    top_detectors: list[tuple[str, float]]   # [(name, raw_score), ...]
    paper_position_opened: bool
    copy_size_cents: int


@dataclass(frozen=True)
class PositionOpenedAlert:
    run_id: int
    position_id: int
    market_question: str
    outcome: str
    size_shares: int
    avg_entry_price_cents: int
    fee_cents: int
    source_wallet: str
    source_flag_id: int | None
    exposure_pct: float                # notional / run balance
    current_balance_cents: int


@dataclass(frozen=True)
class PositionResolvedAlert:
    run_id: int
    position_id: int
    market_question: str
    our_outcome: str
    market_resolved_outcome: str       # YES / NO / INVALID
    size_shares: int
    avg_entry_price_cents: int
    payout_per_share_cents: int        # 100 / 0 / 0-invalid
    realized_pnl_cents: int
    holding_seconds: int


@dataclass(frozen=True)
class DailySummaryAlert:
    as_of_iso: str                     # "2026-04-19"
    runs: list[dict[str, object]]      # [{id, name, pnl_cents, pct, win_rate, acted, flagged}, ...]
    top_category: tuple[str, int] | None
    worst_category: tuple[str, int] | None
    llm_calls_today: int
    llm_cost_cents_today: int
    kill_switches_nominal: bool
    alchemy_calls_today: int | None = None


# ── Sink protocol ────────────────────────────────────────


class AlertSink(Protocol):
    async def send_flag_raised(self, alert: FlagAlert) -> None: ...
    async def send_position_opened(self, alert: PositionOpenedAlert) -> None: ...
    async def send_position_resolved(self, alert: PositionResolvedAlert) -> None: ...
    async def send_daily_summary(self, alert: DailySummaryAlert) -> None: ...


class NullSink:
    """No-op sink used when telegram is disabled or unconfigured."""

    async def send_flag_raised(self, alert: FlagAlert) -> None:
        _ = alert
        return None

    async def send_position_opened(self, alert: PositionOpenedAlert) -> None:
        _ = alert
        return None

    async def send_position_resolved(self, alert: PositionResolvedAlert) -> None:
        _ = alert
        return None

    async def send_daily_summary(self, alert: DailySummaryAlert) -> None:
        _ = alert
        return None


# ── MarkdownV2 helpers ───────────────────────────────────


# Characters that MUST be escaped in MarkdownV2 bodies (Telegram spec).
# Inside a code span a narrower set applies; we only use code spans for
# monospace wallet IDs which are limited to hex.
_MDV2_ESCAPE = re.compile(r"([_\*\[\]\(\)~`>\#\+\-\=\|\{\}\.\!])")


def escape_mdv2(s: str) -> str:
    return _MDV2_ESCAPE.sub(r"\\\1", s)


def _money(cents: int) -> str:
    dollars = cents / 100.0
    if dollars >= 0:
        return f"+\\${dollars:,.2f}"
    return f"\\-\\${-dollars:,.2f}"


def _plain_money(cents: int) -> str:
    """For non-MDV2 contexts (the CLI replays alerts to the console)."""
    dollars = cents / 100.0
    return f"{'+' if dollars >= 0 else ''}${dollars:,.2f}"


def _pct(p: float) -> str:
    return escape_mdv2(f"{p*100:.1f}%")


def _num(x: float | int, spec: str = ".1f") -> str:
    """Format a number then escape MarkdownV2 reserved chars (periods etc.)."""
    return escape_mdv2(format(x, spec))


def _int_or(v: object, default: int) -> int:
    if v is None:
        return default
    if isinstance(v, int):
        return v
    if isinstance(v, float | str | bytes | bytearray):
        try:
            return int(v)
        except (TypeError, ValueError):
            return default
    return default


def _float_or(v: object, default: float) -> float:
    if v is None:
        return default
    if isinstance(v, int | float):
        return float(v)
    if isinstance(v, str | bytes | bytearray):
        try:
            return float(v)
        except (TypeError, ValueError):
            return default
    return default


def _wallet_tag(addr: str, prefix_len: int = 8) -> str:
    a = addr.lower()
    short = a if len(a) <= prefix_len + 3 else f"{a[:prefix_len]}..."
    return f"`{escape_mdv2(short)}`"


# ── Formatters (public so tests can roundtrip without a live bot) ──


def format_flag_raised(alert: FlagAlert) -> str:
    lines: list[str] = []
    verdict = alert.verdict or "unchecked"
    conf = (
        f" \\({_num(alert.verdict_confidence, '.2f')}\\)"
        if alert.verdict_confidence is not None
        else ""
    )
    lines.append(
        f"🚩 *Flag \\#{alert.flag_id}*   composite *{_num(alert.composite_score, '.1f')}*   "
        f"{escape_mdv2(verdict)}{conf}"
    )
    lines.append(f"*Market:* {escape_mdv2(alert.market_question)}")
    lines.append(f"*Wallet:* {_wallet_tag(alert.wallet_address)}")
    notional = (alert.trade_size_shares * alert.trade_price_cents) / 100.0
    lines.append(
        f"*Trade:*  {escape_mdv2(alert.trade_side)} "
        f"{_num(alert.trade_size_shares, ',')} {escape_mdv2(alert.trade_outcome)} "
        f"@ {alert.trade_price_cents}¢  "
        f"\\(\\${_num(notional, ',.2f')}\\)"
    )
    if alert.top_detectors:
        dets = ", ".join(
            f"{escape_mdv2(n)}\\({_num(raw, '.2f')}\\)"
            for n, raw in alert.top_detectors[:3]
        )
        lines.append(f"_Top detectors:_ {dets}")
    if alert.paper_position_opened:
        lines.append(
            f"→ Paper position opened in run \\(copy \\${_num(alert.copy_size_cents/100, ',.2f')}\\)"
        )
    return "\n".join(lines)


def format_position_opened(alert: PositionOpenedAlert) -> str:
    cost = alert.size_shares * alert.avg_entry_price_cents + alert.fee_cents
    lines: list[str] = [
        f"🟢 *Position opened*  · run \\#{alert.run_id}",
        f"{escape_mdv2(alert.market_question)}  *{escape_mdv2(alert.outcome)}*",
        f"{_num(alert.size_shares, ',')} shares @ {alert.avg_entry_price_cents}¢ "
        f"\\(\\${_num(cost/100, ',.2f')} net of \\${_num(alert.fee_cents/100, '.2f')} fee\\)",
        f"_source:_ {_wallet_tag(alert.source_wallet)} · flag \\#{alert.source_flag_id}",
        f"Exposure: \\${_num(alert.current_balance_cents/100, ',.2f')} bankroll, "
        f"this position {_pct(alert.exposure_pct)}",
    ]
    return "\n".join(lines)


def format_position_resolved(alert: PositionResolvedAlert) -> str:
    invalid = alert.market_resolved_outcome == "INVALID"
    won = (not invalid) and (alert.our_outcome == alert.market_resolved_outcome)
    emoji = "🟩" if won else ("🟨" if invalid else "🔴")
    verdict_word = "WON" if won else ("INVALID" if invalid else "LOST")

    hours = alert.holding_seconds / 3600.0
    holding = (
        f"{hours:.1f}h" if hours < 48 else f"{hours/24:.1f}d"
    )

    lines: list[str] = [
        f"{emoji} *Position resolved*  · run \\#{alert.run_id}",
        f"{escape_mdv2(alert.market_question)}  *{escape_mdv2(alert.our_outcome)}*  "
        f"→  *{escape_mdv2(verdict_word)}*",
        f"Bought {_num(alert.size_shares, ',')} @ {alert.avg_entry_price_cents}¢, "
        f"closed at {alert.payout_per_share_cents}¢",
        f"P&L: *{escape_mdv2(_plain_money(alert.realized_pnl_cents))}*  · "
        f"holding {escape_mdv2(holding)}",
    ]
    return "\n".join(lines)


def format_daily_summary(alert: DailySummaryAlert) -> str:
    lines: list[str] = [
        f"📊 *PolySim daily summary* — {escape_mdv2(alert.as_of_iso)}",
    ]
    if alert.runs:
        lines.append(f"Active runs: *{len(alert.runs)}*")
        for r in alert.runs:
            rid = _int_or(r.get("id"), 0)
            name = str(r.get("name") or "")
            pnl = _int_or(r.get("pnl_cents"), 0)
            pct = _float_or(r.get("pct"), 0.0) * 100
            wr = _float_or(r.get("win_rate"), 0.0) * 100
            acted = _int_or(r.get("acted"), 0)
            flagged = _int_or(r.get("flagged"), 0)
            lines.append(
                f"  \\#{rid} {escape_mdv2(name)}  "
                f"*{escape_mdv2(_plain_money(pnl))}* "
                f"\\({escape_mdv2(f'{pct:+.2f}%')}\\)  "
                f"acted {acted}/{flagged}  win {escape_mdv2(f'{wr:.0f}%')}"
            )
    else:
        lines.append("_no active runs_")

    if alert.top_category is not None:
        name, pnl = alert.top_category
        lines.append(f"Top category: *{escape_mdv2(name)}* \\({escape_mdv2(_plain_money(pnl))}\\)")
    if alert.worst_category is not None:
        name, pnl = alert.worst_category
        lines.append(f"Worst: *{escape_mdv2(name)}* \\({escape_mdv2(_plain_money(pnl))}\\)")

    cost_line = (
        f"API: *{alert.llm_calls_today}* calls · "
        f"\\${_num(alert.llm_cost_cents_today/100, '.2f')}"
    )
    lines.append(cost_line)
    lines.append(
        "Kill switches: *"
        + ("all nominal" if alert.kill_switches_nominal else "ATTENTION")
        + "*"
    )
    return "\n".join(lines)


# ── Telegram sink ────────────────────────────────────────


class TelegramSink:
    """Wraps python-telegram-bot's Bot. Never logs the token."""

    def __init__(self, *, bot_token: str, chat_id: str) -> None:
        if not bot_token or not chat_id:
            raise ValueError("bot_token and chat_id are required")
        # Lazy import so unit tests without the lib still run.
        from telegram import Bot

        self._bot: Bot = Bot(token=bot_token)
        self._chat_id = chat_id
        # Do NOT store `bot_token` on self — prevents accidental logging.

    async def _send(self, text: str) -> None:
        try:
            await self._bot.send_message(
                chat_id=self._chat_id,
                text=text,
                parse_mode="MarkdownV2",
                disable_web_page_preview=True,
            )
        except Exception as exc:
            # Never raise into the pipeline — Telegram is best-effort.
            log.warning("telegram send failed: %s", type(exc).__name__)

    async def send_flag_raised(self, alert: FlagAlert) -> None:
        await self._send(format_flag_raised(alert))

    async def send_position_opened(self, alert: PositionOpenedAlert) -> None:
        await self._send(format_position_opened(alert))

    async def send_position_resolved(self, alert: PositionResolvedAlert) -> None:
        await self._send(format_position_resolved(alert))

    async def send_daily_summary(self, alert: DailySummaryAlert) -> None:
        await self._send(format_daily_summary(alert))


def make_sink_from_config(
    *,
    enabled: bool,
    bot_token: str,
    chat_id: str,
) -> AlertSink:
    """Factory: returns TelegramSink iff enabled + configured, else NullSink."""
    if not enabled or not bot_token or not chat_id:
        return NullSink()
    try:
        return TelegramSink(bot_token=bot_token, chat_id=chat_id)
    except Exception as exc:
        log.warning("telegram sink disabled: %s", type(exc).__name__)
        return NullSink()
