"""Inbound Telegram bot — addendum B1 tests.

Exercises every command via `dispatch_command`, without spinning up
python-telegram-bot.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from polysim.db import dao
from polysim.db.migrations.runner import apply_migrations
from polysim.profiles import load_profile
from polysim.reporter.telegram_bot import build_command_handlers, dispatch_command


@pytest.fixture
async def db(tmp_path: Path) -> Path:
    path = tmp_path / "t.db"
    await apply_migrations(path)
    return path


async def test_help_lists_all_commands(db: Path) -> None:
    h = build_command_handlers(db)
    reply = await dispatch_command("/help", h)
    assert reply is not None
    for tok in ("/status", "/flags", "/runs", "/run", "/compare", "/report"):
        assert tok in reply


async def test_unknown_command_gives_hint(db: Path) -> None:
    h = build_command_handlers(db)
    reply = await dispatch_command("/nope", h)
    assert reply is not None
    assert "unknown command" in reply
    assert "/help" in reply


async def test_non_command_returns_none(db: Path) -> None:
    h = build_command_handlers(db)
    reply = await dispatch_command("hello there", h)
    assert reply is None


async def test_start_aliases_help(db: Path) -> None:
    h = build_command_handlers(db)
    reply = await dispatch_command("/start", h)
    assert reply is not None
    assert "/status" in reply


async def test_status_on_empty_db(db: Path) -> None:
    h = build_command_handlers(db)
    reply = await dispatch_command("/status", h)
    assert reply is not None
    assert "trades" in reply
    assert "active runs" in reply


async def test_runs_and_run_with_seeded_run(db: Path) -> None:
    p = load_profile("systematic")
    rid = await dao.create_paper_run(
        db, name="demo", starting_balance_cents=1_000_000,
        config_snapshot={}, profile_name=p.name,
        profile_snapshot=p.model_dump(), tag="test",
    )
    h = build_command_handlers(db)
    runs_reply = await dispatch_command("/runs", h)
    assert runs_reply is not None
    assert f"#{rid}" in runs_reply
    assert "systematic" in runs_reply

    detail = await dispatch_command(f"/run {rid}", h)
    assert detail is not None
    assert "balance" in detail


async def test_compare_with_tag(db: Path) -> None:
    for name in ("systematic", "medium"):
        p = load_profile(name)
        await dao.create_paper_run(
            db, name=f"d-{name}", starting_balance_cents=1_000_000,
            config_snapshot={}, profile_name=p.name,
            profile_snapshot=p.model_dump(), tag="exp",
        )
    h = build_command_handlers(db)
    reply = await dispatch_command("/compare exp", h)
    assert reply is not None
    assert "systematic" in reply
    assert "medium" in reply


async def test_report_missing_run(db: Path) -> None:
    h = build_command_handlers(db)
    reply = await dispatch_command("/report 999", h)
    assert reply is not None
    assert "not found" in reply


async def test_report_usage_on_missing_arg(db: Path) -> None:
    h = build_command_handlers(db)
    for cmd in ("/report", "/run", "/compare"):
        reply = await dispatch_command(cmd, h)
        assert reply is not None
        assert "usage" in reply


async def test_flags_bad_since_returns_error(db: Path) -> None:
    h = build_command_handlers(db)
    reply = await dispatch_command("/flags since=foo", h)
    assert reply is not None
    assert "bad since" in reply


async def test_command_with_botname_suffix(db: Path) -> None:
    # Telegram can deliver /help@PolysimBot — handler must strip the suffix.
    h = build_command_handlers(db)
    reply = await dispatch_command("/help@PolysimBot", h)
    assert reply is not None
    assert "/status" in reply
