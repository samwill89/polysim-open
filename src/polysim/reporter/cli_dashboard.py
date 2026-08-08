"""Rich TUI dashboard — build plan §6.1 / §6.2.

Layout matches polysim-demo-annotated.html panel #2:

  +----------------------------------------------------+
  | LIVE FLAGS             |  OPEN POSITIONS           |
  +------------------------+---------------------------+
  | P&L BY CATEGORY        |  KILL SWITCHES            |
  |                        |  SOURCE WALLETS           |
  +------------------------+---------------------------+
  | INGEST HEARTBEAT       |  BASELINE COMPARISON      |
  +----------------------------------------------------+

Refresh cadence (configurable): 500ms for volatile panels (flags,
positions, heartbeat), 2s for aggregate metric panels (P&L, baselines).
Quit on Ctrl+C; popup detail views delivered as one-shot sub-commands
(see cli.py dashboard --pop-*).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from polysim.db import dao
from polysim.utils.time import iso, now_utc

log = logging.getLogger(__name__)


# ── state snapshot ───────────────────────────────────────


@dataclass
class DashboardState:
    flags: list[dict[str, Any]] = field(default_factory=list)
    open_positions: list[dict[str, Any]] = field(default_factory=list)
    pnl_by_category: dict[str, int] = field(default_factory=dict)
    source_wallets_top: list[tuple[str, int, int]] = field(default_factory=list)
    # Heartbeat
    trades_last_1h: int = 0
    last_trade_at: datetime | None = None
    # Kill switches
    runs_paused: int = 0
    flags_last_hour: int = 0
    max_flags_per_hour: int = 100
    drawdown_pct: float = 0.0
    drawdown_limit_pct: float = 0.20
    llm_calls_today: int = 0
    llm_max_per_day: int = 100
    # Baselines (set only when a report exists; else stays None)
    baseline_status: str = "—"


async def collect_state(
    db_path: Path, run_id: int | None
) -> DashboardState:
    """Single snapshot of everything the dashboard shows."""
    state = DashboardState()

    # Recent flags
    flags_since = iso(now_utc() - timedelta(hours=24))
    state.flags = (
        await dao.list_flags_since(
            db_path, since_iso=flags_since, category=None, limit=12
        )
    )
    # Flag rate kill-switch input
    state.flags_last_hour = await dao.count_flags_since(
        db_path, since_iso=iso(now_utc() - timedelta(hours=1))
    )

    if run_id is not None:
        state.open_positions = await dao.list_open_positions(db_path, run_id)

        # Source wallet exposure (top 5)
        by_wallet: dict[str, tuple[int, int]] = {}
        for pos in state.open_positions:
            w = str(pos.get("source_wallet") or "")
            if not w:
                continue
            notional = int(pos.get("size_shares") or 0) * int(
                pos.get("avg_entry_price_cents") or 0
            )
            ex, count = by_wallet.get(w, (0, 0))
            by_wallet[w] = (ex + notional, count + 1)
        state.source_wallets_top = sorted(
            ((w, ex, c) for w, (ex, c) in by_wallet.items()),
            key=lambda t: -t[1],
        )[:5]

        # P&L per category via closed + open (open = 0 unrealized).
        import aiosqlite
        async with (
            aiosqlite.connect(str(db_path)) as db,
            db.execute(
                "SELECT p.market_id, p.realized_pnl_cents, m.category "
                "FROM paper_positions p JOIN markets m ON m.id = p.market_id "
                "WHERE p.run_id = ? AND p.status IN ('CLOSED','RESOLVED')",
                (run_id,),
            ) as cur,
        ):
            db.row_factory = aiosqlite.Row
            for raw in await cur.fetchall():
                cat = str(raw[2] or "unknown")
                state.pnl_by_category[cat] = state.pnl_by_category.get(
                    cat, 0
                ) + int(raw[1] or 0)

        # Run + drawdown
        run = await dao.get_paper_run(db_path, run_id)
        if run is not None:
            start = int(run.get("starting_balance_cents") or 0)
            current = int(run.get("current_balance_cents") or 0)
            if start > 0:
                state.drawdown_pct = max(0.0, (start - current) / start)
            if run.get("paused_at"):
                state.runs_paused = 1

    # Heartbeat
    from polysim.utils.watchdog import latest_trade_timestamp

    state.last_trade_at = await latest_trade_timestamp(db_path)
    one_hour_ago = iso(now_utc() - timedelta(hours=1))
    import aiosqlite
    try:
        async with (
            aiosqlite.connect(str(db_path)) as db,
            db.execute(
                "SELECT COUNT(*) FROM trades WHERE timestamp >= ?",
                (one_hour_ago,),
            ) as cur,
        ):
            row = await cur.fetchone()
            state.trades_last_1h = int(row[0]) if row and row[0] is not None else 0
    except aiosqlite.OperationalError:
        state.trades_last_1h = 0

    # LLM calls today
    cost = await dao.sum_flag_costs_since(
        db_path, since_iso=iso(now_utc() - timedelta(hours=24))
    )
    state.llm_calls_today = cost.get("total_calls", 0)

    return state


# ── panel renderers (pure functions of state) ─────────────


def render_flags_panel(state: DashboardState) -> Panel:
    table = Table.grid(padding=(0, 1))
    table.add_column(justify="right")
    table.add_column(justify="left")
    table.add_column(justify="left")
    table.add_column(justify="right")
    for f in state.flags[:10]:
        fid = int(f.get("id") or 0)
        ts = str(f.get("created_at") or "")[11:16]  # HH:MM
        cat = str(f.get("category") or "-")[:7]
        score = float(f.get("composite_score") or 0.0)
        verdict = (f.get("investigator_verdict") or "").upper()
        colour = "green" if verdict == "INFORMED" else "yellow"
        table.add_row(
            Text(f"#{fid}"),
            Text(ts),
            Text(f"{cat:<7}", style="dim"),
            Text(f"{score:.1f} {verdict}", style=colour if verdict else ""),
        )
    if not state.flags:
        table.add_row(Text("(none)", style="dim"), Text(""), Text(""), Text(""))
    return Panel(table, title="LIVE FLAGS", border_style="cyan")


def render_positions_panel(state: DashboardState) -> Panel:
    table = Table.grid(padding=(0, 1))
    table.add_column(justify="left")
    table.add_column(justify="right")
    table.add_column(justify="left")
    for pos in state.open_positions[:10]:
        mid = str(pos.get("market_id") or "")[:18]
        size = int(pos.get("size_shares") or 0)
        outcome = str(pos.get("outcome") or "")
        notional = size * int(pos.get("avg_entry_price_cents") or 0)
        table.add_row(
            Text(mid),
            Text(f"{size:,} {outcome}"),
            Text(f"${notional/100:,.2f}", style="cyan"),
        )
    if not state.open_positions:
        table.add_row(Text("(none)", style="dim"), Text(""), Text(""))
    return Panel(table, title="OPEN POSITIONS", border_style="green")


def render_pnl_panel(state: DashboardState) -> Panel:
    table = Table.grid(padding=(0, 2))
    table.add_column(justify="left")
    table.add_column(justify="right")
    rows = sorted(state.pnl_by_category.items(), key=lambda kv: -kv[1])
    if not rows:
        table.add_row(Text("(no closed positions yet)", style="dim"), Text(""))
    for cat, pnl in rows:
        sign = "+" if pnl >= 0 else ""
        colour = "green" if pnl >= 0 else "red"
        table.add_row(
            Text(cat),
            Text(f"{sign}${pnl/100:,.2f}", style=colour),
        )
    return Panel(table, title="P&L BY CATEGORY (closed)", border_style="magenta")


def render_kill_switches_panel(state: DashboardState) -> Panel:
    table = Table.grid(padding=(0, 2))
    table.add_column(justify="left")
    table.add_column(justify="right")

    dd_colour = "red" if state.drawdown_pct > state.drawdown_limit_pct else "green"
    table.add_row(
        Text("Drawdown"),
        Text(
            f"{state.drawdown_pct*100:.1f}% / {state.drawdown_limit_pct*100:.0f}%",
            style=dd_colour,
        ),
    )
    fr_colour = "red" if state.flags_last_hour > state.max_flags_per_hour else "green"
    table.add_row(
        Text("Flag rate (1h)"),
        Text(
            f"{state.flags_last_hour} / {state.max_flags_per_hour}",
            style=fr_colour,
        ),
    )
    table.add_row(
        Text("Paused runs"),
        Text(
            f"{state.runs_paused}",
            style="red" if state.runs_paused else "green",
        ),
    )
    table.add_row(
        Text("Investigator"),
        Text(f"{state.llm_calls_today} / {state.llm_max_per_day}"),
    )
    return Panel(table, title="KILL SWITCHES", border_style="yellow")


def render_source_wallets_panel(state: DashboardState) -> Panel:
    table = Table.grid(padding=(0, 1))
    table.add_column(justify="left")
    table.add_column(justify="right")
    table.add_column(justify="right")
    for w, ex, count in state.source_wallets_top:
        short = f"{w[:8]}..."
        table.add_row(Text(short, style="cyan"), Text(f"${ex/100:,.2f}"), Text(f"{count}"))
    if not state.source_wallets_top:
        table.add_row(Text("(none)", style="dim"), Text(""), Text(""))
    return Panel(table, title="SOURCE WALLETS (top 5)", border_style="cyan")


def render_heartbeat_panel(state: DashboardState) -> Panel:
    lines: list[str] = []
    if state.last_trade_at is None:
        lines.append("[dim]no trades in DB yet[/dim]")
    else:
        age = (now_utc() - state.last_trade_at).total_seconds()
        colour = "green" if age < 60 else "yellow" if age < 300 else "red"
        lines.append(
            f"Last trade: [{colour}]{int(age)}s ago[/{colour}]"
        )
    lines.append(f"Trades/hour: {state.trades_last_1h}")
    lines.append(f"Now: {now_utc().strftime('%Y-%m-%d %H:%M UTC')}")
    text = Text.from_markup("\n".join(lines))
    return Panel(text, title="INGEST HEARTBEAT", border_style="dim")


def render_baseline_panel(state: DashboardState) -> Panel:
    text = Text(state.baseline_status or "—", style="dim")
    return Panel(
        text,
        title="BASELINE COMPARISON",
        subtitle="run `polysim report --run N` to refresh",
        border_style="dim",
    )


def build_layout(state: DashboardState) -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=1),
        Layout(name="top"),
        Layout(name="mid"),
        Layout(name="bottom"),
    )
    layout["header"].update(
        Text(
            f"PolySim Dashboard  ·  {datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S UTC')}",
            style="bold magenta",
        )
    )
    layout["top"].split_row(
        Layout(name="flags", ratio=1),
        Layout(name="positions", ratio=1),
    )
    layout["mid"].split_row(
        Layout(name="pnl", ratio=1),
        Layout(name="risk"),
    )
    layout["mid"]["risk"].split_column(
        Layout(name="switches"),
        Layout(name="wallets"),
    )
    layout["bottom"].split_row(
        Layout(name="heartbeat", ratio=1),
        Layout(name="baseline", ratio=1),
    )

    layout["top"]["flags"].update(render_flags_panel(state))
    layout["top"]["positions"].update(render_positions_panel(state))
    layout["mid"]["pnl"].update(render_pnl_panel(state))
    layout["mid"]["risk"]["switches"].update(render_kill_switches_panel(state))
    layout["mid"]["risk"]["wallets"].update(render_source_wallets_panel(state))
    layout["bottom"]["heartbeat"].update(render_heartbeat_panel(state))
    layout["bottom"]["baseline"].update(render_baseline_panel(state))
    return layout


# ── runner ───────────────────────────────────────────────


async def collect_split_state(
    db_path: Path, run_ids: list[int]
) -> list[tuple[dict[str, Any], DashboardState]]:
    """One DashboardState per run, for the split-pane comparative view."""
    out: list[tuple[dict[str, Any], DashboardState]] = []
    for rid in run_ids:
        run = await dao.get_paper_run(db_path, rid)
        if run is None:
            continue
        state = await collect_state(db_path, rid)
        out.append((run, state))
    return out


def build_split_layout(
    panes: list[tuple[dict[str, Any], DashboardState]]
) -> Layout:
    """Addendum §6 comparative view — one column per run."""
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=1),
        Layout(name="body"),
    )
    names = " | ".join(
        f"{run.get('profile_name') or '?'} #{run['id']}"
        for run, _ in panes
    )
    layout["header"].update(
        Text(
            f"PolySim Compare  ·  {names}  ·  "
            f"{datetime.now(UTC).strftime('%H:%M:%S UTC')}",
            style="bold magenta",
        )
    )
    if not panes:
        layout["body"].update(Text("(no runs)", style="dim"))
        return layout
    # N columns, one per run.
    cols = [Layout(name=f"col_{i}") for i in range(len(panes))]
    layout["body"].split_row(*cols)
    for (run, state), col in zip(panes, cols, strict=True):
        col.split_column(
            Layout(name="summary", size=12),
            Layout(name="positions", ratio=1),
            Layout(name="switches", size=6),
        )
        col["summary"].update(_render_split_summary(run, state))
        col["positions"].update(render_positions_panel(state))
        col["switches"].update(render_kill_switches_panel(state))
    return layout


def _render_split_summary(run: dict[str, Any], state: DashboardState) -> Panel:
    start = int(run.get("starting_balance_cents") or 0)
    cur = int(run.get("current_balance_cents") or 0)
    pnl_pct = 100.0 * (cur - start) / start if start else 0.0
    lines = [
        f"[bold]{run.get('profile_name') or '?'}[/bold]   run #{run['id']}",
        f"Balance:  ${cur/100:,.0f}  ({pnl_pct:+.1f}%)",
        f"Start:    ${start/100:,.0f}",
        f"Open:     {len(state.open_positions)}",
        f"Flags 1h: {state.flags_last_hour}",
    ]
    if run.get("paused_at"):
        lines.append(f"[red]PAUSED[/red] {run.get('pause_reason') or ''}")
    return Panel(
        Text.from_markup("\n".join(lines)),
        title=str(run.get("name") or "-"),
        border_style="cyan",
    )


async def run_split_dashboard(
    db_path: Path,
    *,
    run_ids: list[int],
    fast_refresh_s: float = 2.0,
    max_ticks: int | None = None,
) -> None:
    console = Console()
    panes = await collect_split_state(db_path, run_ids)
    tick = 0
    with Live(build_split_layout(panes), console=console, refresh_per_second=1) as live:
        while max_ticks is None or tick < max_ticks:
            tick += 1
            try:
                panes = await collect_split_state(db_path, run_ids)
                live.update(build_split_layout(panes))
            except Exception as exc:
                log.warning("split dashboard collect failed: %s", exc)
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.sleep(fast_refresh_s)


async def run_dashboard(
    db_path: Path,
    *,
    run_id: int | None = None,
    fast_refresh_s: float = 0.5,
    slow_refresh_s: float = 2.0,
    max_ticks: int | None = None,
) -> None:
    """Run the live dashboard until Ctrl+C.

    `max_ticks`: optional cap for tests — after this many refresh ticks
    the loop returns cleanly (the real-world invocation passes None and
    blocks until SIGINT).
    """
    console = Console()
    state = await collect_state(db_path, run_id)
    tick = 0
    _ = fast_refresh_s, slow_refresh_s

    with Live(build_layout(state), console=console, refresh_per_second=2) as live:
        while max_ticks is None or tick < max_ticks:
            tick += 1
            try:
                state = await collect_state(db_path, run_id)
                live.update(build_layout(state))
            except Exception as exc:
                log.warning("dashboard collect_state failed: %s", exc)
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.sleep(fast_refresh_s)
