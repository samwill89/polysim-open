"""TUI panel rendering tests — build plan §6.1."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from polysim.db import dao
from polysim.db.migrations.runner import apply_migrations
from polysim.models import Flag, Market, TradeEvent
from polysim.reporter.cli_dashboard import (
    DashboardState,
    build_layout,
    collect_state,
    render_flags_panel,
    render_heartbeat_panel,
    render_kill_switches_panel,
    render_pnl_panel,
)


@pytest.fixture
async def db(tmp_path: Path) -> Path:
    path = tmp_path / "t.db"
    await apply_migrations(path)
    return path


def test_build_layout_renders_with_empty_state() -> None:
    state = DashboardState()
    layout = build_layout(state)
    # Layout exposes `.tree` attribute post-split; sanity-check a render
    from rich.console import Console

    console = Console(record=True, width=120)
    console.print(layout)
    out = console.export_text()
    assert "LIVE FLAGS" in out
    assert "OPEN POSITIONS" in out
    assert "KILL SWITCHES" in out


def test_flags_panel_empty_state() -> None:
    panel = render_flags_panel(DashboardState())
    from rich.console import Console

    console = Console(record=True, width=80)
    console.print(panel)
    assert "(none)" in console.export_text()


def test_kill_switches_panel_red_when_drawdown_over_limit() -> None:
    state = DashboardState(drawdown_pct=0.25, drawdown_limit_pct=0.20)
    panel = render_kill_switches_panel(state)
    from rich.console import Console

    console = Console(record=True, width=80)
    console.print(panel)
    # Bar at 25% / 20% — output must include both numbers
    out = console.export_text()
    assert "25.0" in out
    assert "20" in out


def test_heartbeat_panel_without_trades() -> None:
    panel = render_heartbeat_panel(DashboardState())
    from rich.console import Console

    console = Console(record=True, width=80)
    console.print(panel)
    assert "no trades" in console.export_text()


def test_pnl_panel_sorted_by_pnl() -> None:
    state = DashboardState(
        pnl_by_category={"ai": 5000, "aec": -1000, "creator": 2000}
    )
    panel = render_pnl_panel(state)
    from rich.console import Console

    console = Console(record=True, width=60)
    console.print(panel)
    out = console.export_text()
    ai_pos = out.index("ai")
    creator_pos = out.index("creator")
    aec_pos = out.index("aec")
    assert ai_pos < creator_pos < aec_pos


async def test_collect_state_populates_from_db(db: Path) -> None:
    # Seed a flag + open position.
    await dao.upsert_market(
        db,
        Market(
            id="m1", slug="s", question="?",
            category="ai",  # type: ignore[arg-type]
            created_at=datetime(2026, 4, 1, tzinfo=UTC),
        ),
    )
    await dao.upsert_wallet_first_sight(db, "0xaf4")
    await dao.write_flag(
        db,
        Flag(
            wallet_address="0xaf4",
            market_id="m1",
            detector_name="composite",
            raw_score=7.0,
            composite_score=7.5,
            components={},
            created_at=datetime.now(UTC),
        ),
    )
    run_id = await dao.create_paper_run(
        db, name="t", starting_balance_cents=1_000_000, config_snapshot={}
    )
    await dao.write_paper_position(
        db, run_id=run_id, market_id="m1", outcome="YES",
        size_shares=100, avg_entry_price_cents=40,
        source_flag_id=None, source_wallet="0xaf4",
    )
    # Ingest a trade so the heartbeat panel has data.
    await dao.insert_trades_batch(
        db,
        [TradeEvent(
            id="t1", wallet_address="0xaf4", market_id="m1",
            side="BUY", outcome="YES", size_shares=100, price_cents=40,
            timestamp=datetime.now(UTC) - timedelta(seconds=30),
        )],
    )

    state = await collect_state(db, run_id)
    assert len(state.flags) == 1
    assert len(state.open_positions) == 1
    assert state.trades_last_1h >= 1
    assert state.last_trade_at is not None
