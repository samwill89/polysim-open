"""Inbound Telegram bot — commands /status /flags /runs /run /compare
/report /help.

Spec §2 hard-bans a web UI. This is NOT a web UI — it's a text-only,
read-only command surface scoped to a single chat_id. No trading actions
are exposed (nothing to open positions, modify profiles, or touch keys).

Auth model: Every incoming message is rejected unless chat.id matches the
configured `authorized_chat_id`. This is deliberately simple — PolySim is
single-operator by spec.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from datetime import timedelta
from pathlib import Path

from polysim.db import dao
from polysim.reporter.telegram import escape_mdv2
from polysim.utils.time import iso, now_utc, parse_since

log = logging.getLogger(__name__)

# Signature of the per-command handler: returns the text to reply with.
CommandFn = Callable[[list[str]], Awaitable[str]]


def build_command_handlers(db_path: Path) -> dict[str, CommandFn]:
    """Map "/cmd" -> handler. Separated from the bot class so unit tests
    can exercise them without spinning up python-telegram-bot."""

    async def _status(args: list[str]) -> str:
        _ = args
        stats = await dao.db_stats(db_path)
        runs = await dao.list_active_runs(db_path)
        flags_hr = await dao.count_flags_since(
            db_path, since_iso=iso(now_utc() - timedelta(hours=1))
        )
        lines = [
            "*PolySim status*",
            f"trades: {stats['trades']:,}   wallets: {stats['wallets']:,}",
            f"markets: {stats['markets']:,}   flags: {stats['flags']:,}",
            f"active runs: {len(runs)}",
            f"flags last 1h: {flags_hr}",
        ]
        return "\n".join(escape_mdv2(ln) if "*" not in ln else ln for ln in lines)

    async def _flags(args: list[str]) -> str:
        # /flags [since=24h] [limit=10]
        since = "24h"
        limit = 10
        for a in args:
            if a.startswith("since="):
                since = a.split("=", 1)[1]
            elif a.startswith("limit="):
                with contextlib.suppress(ValueError):
                    limit = int(a.split("=", 1)[1])
        try:
            delta = parse_since(since)
        except ValueError:
            return f"bad since arg: {escape_mdv2(since)}"
        rows = await dao.list_flags_since(
            db_path,
            since_iso=iso(now_utc() - delta),
            category=None,
            limit=limit,
        )
        if not rows:
            return f"_no flags in last {escape_mdv2(since)}_"
        lines = [f"*Flags last {escape_mdv2(since)}* \\(top {limit}\\)"]
        for r in rows:
            fid = int(r.get("id") or 0)
            score = float(r.get("composite_score") or 0.0)
            verdict = str(r.get("investigator_verdict") or "-")
            cat = str(r.get("category") or "-")
            lines.append(
                f"\\#{fid}  {escape_mdv2(cat)}  "
                f"{escape_mdv2(f'{score:.1f}')}  "
                f"{escape_mdv2(verdict)}"
            )
        return "\n".join(lines)

    async def _runs(args: list[str]) -> str:
        _ = args
        rows = await dao.list_paper_runs(db_path, limit=25)
        if not rows:
            return "_no runs_"
        lines = ["*Runs*"]
        for r in rows[:15]:
            rid = int(r["id"])
            start = int(r.get("starting_balance_cents") or 0)
            cur = int(r.get("current_balance_cents") or 0)
            ret = 100.0 * (cur - start) / start if start else 0.0
            profile = str(r.get("profile_name") or "-")
            tag = str(r.get("tag") or "")
            status = (
                "ended" if r.get("ended_at")
                else "PAUSED" if r.get("paused_at")
                else "active"
            )
            tagstr = f" `#{escape_mdv2(tag)}`" if tag else ""
            lines.append(
                f"\\#{rid} `{escape_mdv2(profile)}` "
                f"{escape_mdv2(f'{ret:+.1f}%')}   {escape_mdv2(status)}{tagstr}"
            )
        return "\n".join(lines)

    async def _run(args: list[str]) -> str:
        if not args:
            return "usage: /run <id>"
        try:
            rid = int(args[0])
        except ValueError:
            return "usage: /run <id>"
        from polysim.paper.run_manager import run_status

        s = await run_status(db_path, rid)
        if "error" in s:
            return escape_mdv2(s["error"])
        bal_now = f"${s['current_balance_cents']/100:,.2f}"
        bal_start = f"${s['starting_balance_cents']/100:,.2f}"
        realized = f"${s['realized_pnl_cents']/100:,.2f}"
        lines = [
            f"*Run \\#{rid}*  `{escape_mdv2(str(s.get('name') or '-'))}`",
            f"started {escape_mdv2(str(s.get('started_at') or '-'))}",
        ]
        if s.get("paused"):
            lines.append(
                f"*PAUSED:* {escape_mdv2(str(s.get('paused_reason') or '?'))}"
            )
        lines.append(
            f"balance {escape_mdv2(bal_now)}  start {escape_mdv2(bal_start)}"
        )
        lines.append(
            f"open {s['open_positions']} · realized {escape_mdv2(realized)}"
        )
        return "\n".join(lines)

    async def _compare(args: list[str]) -> str:
        if not args:
            return "usage: /compare <tag>"
        tag = args[0]
        from polysim.evaluator.metrics import compute_run_metrics

        rows = await dao.list_paper_runs_by_tag(db_path, tag=tag)
        if not rows:
            return f"_no runs with tag_ `{escape_mdv2(tag)}`"
        out = [f"*Compare* `{escape_mdv2(tag)}`"]
        for r in rows:
            m = await compute_run_metrics(db_path, int(r["id"]))
            start = int(r.get("starting_balance_cents") or 0)
            cur = int(r.get("current_balance_cents") or 0)
            ret = 100.0 * (cur - start) / start if start else 0.0
            sharpe_s = f"{float(m.get('sharpe_annualized') or 0):.2f}"
            ret_s = f"{ret:+.1f}%"
            out.append(
                f"\\#{r['id']} `{escape_mdv2(r.get('profile_name') or '-')}` "
                f"{escape_mdv2(ret_s)} · "
                f"sharpe {escape_mdv2(sharpe_s)} · "
                f"trades {int(m.get('closed_positions') or 0)}"
            )
        return "\n".join(out)

    async def _report(args: list[str]) -> str:
        if not args:
            return "usage: /report <run_id>"
        try:
            rid = int(args[0])
        except ValueError:
            return "usage: /report <run_id>"
        from polysim.evaluator.metrics import compute_run_metrics

        m = await compute_run_metrics(db_path, rid)
        if not m.get("run_name") or m.get("run_name") == "unknown":
            return f"run \\#{rid} not found"
        ret_s = f"{float(m.get('net_return_pct') or 0) * 100:+.1f}%"
        sharpe_s = f"{float(m.get('sharpe_annualized') or 0):.2f}"
        dd_s = f"{100 * float(m.get('max_drawdown_pct') or 0):.1f}%"
        wr_s = f"{100 * float(m.get('win_rate') or 0):.0f}%"
        out = [
            f"*Report \\#{rid}*  `{escape_mdv2(str(m.get('run_name')))}`",
            f"return: {escape_mdv2(ret_s)}",
            f"sharpe: {escape_mdv2(sharpe_s)}",
            f"max DD: {escape_mdv2(dd_s)}",
            f"trades: {int(m.get('closed_positions') or 0)} · "
            f"win rate: {escape_mdv2(wr_s)}",
        ]
        return "\n".join(out)

    async def _help(args: list[str]) -> str:
        _ = args
        # MarkdownV2 reserves '>', '.', '-', '_', '(' etc. — escape every
        # literal. '<' is not reserved but we escape both angles for symmetry.
        return (
            "*PolySim bot commands* \\(read\\-only\\)\n"
            "/status \\— system stats\n"
            "/flags \\[since\\=24h\\] \\[limit\\=10\\] \\— recent flags\n"
            "/runs \\— paper runs \\(profile, return, tag\\)\n"
            "/run \\<id\\> \\— one run's detail\n"
            "/compare \\<tag\\> \\— all runs with that tag\n"
            "/report \\<run\\_id\\> \\— headline metrics\n"
            "/help \\— this message"
        )

    return {
        "/status": _status,
        "/flags": _flags,
        "/runs": _runs,
        "/run": _run,
        "/compare": _compare,
        "/report": _report,
        "/help": _help,
        "/start": _help,
    }


async def dispatch_command(
    text: str, handlers: dict[str, CommandFn]
) -> str | None:
    """Parse `text` as "/cmd arg1 arg2 ..." and dispatch. None = not a command."""
    text = text.strip()
    if not text.startswith("/"):
        return None
    parts = text.split()
    cmd = parts[0].lower().split("@", 1)[0]  # strip @BotName suffix
    args = parts[1:]
    handler = handlers.get(cmd)
    if handler is None:
        return f"unknown command: `{escape_mdv2(cmd)}`  try /help"
    try:
        return await handler(args)
    except Exception as exc:  # never crash the bot
        log.exception("handler for %s failed", cmd)
        return f"error: {escape_mdv2(type(exc).__name__)}"


class InboundTelegramBot:
    """Long-polling inbound bot. Read-only; chat-ID scoped.

    Runs under `asyncio.create_task(bot.run())`. `stop()` cancels cleanly.
    """

    def __init__(
        self,
        *,
        bot_token: str,
        authorized_chat_id: str,
        db_path: Path,
        poll_interval_s: float = 1.0,
    ) -> None:
        if not bot_token or not authorized_chat_id:
            raise ValueError("bot_token and authorized_chat_id are required")
        self._token = bot_token
        self._chat_id = str(authorized_chat_id)
        self._db = db_path
        self._poll = poll_interval_s
        self._stop = asyncio.Event()
        self._handlers = build_command_handlers(db_path)
        self._offset: int | None = None

    async def run(self) -> None:
        # Lazy import python-telegram-bot so tests can build handlers
        # without the dependency.
        from telegram import Bot

        bot = Bot(token=self._token)
        log.info("inbound bot starting; authorized chat=%s", self._chat_id)
        try:
            while not self._stop.is_set():
                try:
                    updates = await bot.get_updates(
                        offset=self._offset,
                        timeout=10,
                        allowed_updates=["message"],
                    )
                except Exception as exc:
                    log.warning("get_updates failed: %s", type(exc).__name__)
                    await asyncio.sleep(max(1.0, self._poll))
                    continue
                for upd in updates:
                    self._offset = upd.update_id + 1
                    msg = getattr(upd, "message", None)
                    if msg is None or msg.text is None:
                        continue
                    # Chat-ID gate (spec: single operator).
                    sender_chat = str(msg.chat.id) if msg.chat else ""
                    if sender_chat != self._chat_id:
                        log.info(
                            "rejecting message from unauthorized chat=%s",
                            sender_chat,
                        )
                        continue
                    reply = await dispatch_command(msg.text, self._handlers)
                    if reply is None:
                        continue
                    try:
                        await bot.send_message(
                            chat_id=self._chat_id,
                            text=reply,
                            parse_mode="MarkdownV2",
                            disable_web_page_preview=True,
                        )
                    except Exception as exc:
                        log.warning("send reply failed: %s", type(exc).__name__)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=self._poll)
        finally:
            log.info("inbound bot stopped")

    async def stop(self) -> None:
        self._stop.set()
