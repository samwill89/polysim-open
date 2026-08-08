"""`polysim live` orchestrator — B2 lifecycle tests.

Starts the orchestrator with ingest + inbound bot disabled, seeds a flag,
and verifies:
    * N paper runs were created (one per profile)
    * The FlagDispatcherLoop consumed the new flag
    * stop() releases cleanly
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from polysim.config import CategoryEntry, Config, RunConfig, Secrets
from polysim.db import dao
from polysim.db.migrations.runner import apply_migrations
from polysim.live import LiveConfig, LiveOrchestrator
from polysim.models import Flag, Market


def _minimal_config() -> Config:
    return Config(
        run=RunConfig(name="live-test", starting_balance_cents=1_000_000),
        categories={
            "ai": CategoryEntry(enabled=True, tier="primary"),
            "other": CategoryEntry(enabled=True, tier="secondary"),
        },
    )


def _minimal_secrets() -> Secrets:
    s = Secrets()
    # Blank telegram creds -> NullSink, no inbound bot start.
    s.TELEGRAM_BOT_TOKEN = ""
    s.TELEGRAM_CHAT_ID = ""
    return s


@pytest.fixture
async def db(tmp_path: Path) -> Path:
    p = tmp_path / "t.db"
    await apply_migrations(p)
    return p


async def test_orchestrator_starts_one_run_per_profile(db: Path) -> None:
    cfg = _minimal_config()
    orch = LiveOrchestrator(
        db_path=db, cfg=cfg, secrets=_minimal_secrets(),
        live_cfg=LiveConfig(
            profiles=["systematic", "medium", "degen"],
            tag="live-exp",
            balance_cents=1_000_000,
            enable_ingest=False,
            enable_inbound_bot=False,
            enable_daily_summary=False,
            flag_poll_interval_s=0.1,
            resolution_interval_s=60.0,
        ),
    )
    await orch.start()
    try:
        assert len(orch.run_ids) == 3
        tagged = await dao.list_paper_runs_by_tag(db, tag="live-exp")
        profile_names = {r["profile_name"] for r in tagged}
        assert profile_names == {"systematic", "medium", "degen"}
    finally:
        await orch.stop()


async def test_orchestrator_dispatches_newly_created_flags(db: Path) -> None:
    cfg = _minimal_config()
    await dao.upsert_market(
        db,
        Market(
            id="m1", slug="s", question="?",
            category="ai",  # type: ignore[arg-type]
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            daily_volume_usd_cents=500_000,
        ),
    )
    await dao.upsert_wallet_first_sight(db, "0xaf4")

    orch = LiveOrchestrator(
        db_path=db, cfg=cfg, secrets=_minimal_secrets(),
        live_cfg=LiveConfig(
            profiles=["systematic"],
            enable_ingest=False,
            enable_inbound_bot=False,
            enable_daily_summary=False,
            flag_poll_interval_s=0.05,
            resolution_interval_s=60.0,
        ),
    )
    await orch.start()
    try:
        # Insert a flag AFTER start — the watermark should then pick it up.
        fid = await dao.write_flag(
            db,
            Flag(
                wallet_address="0xaf4", market_id="m1",
                detector_name="composite", raw_score=7.0,
                composite_score=7.0,
                components={"contributing_detectors": ["CategoryInsiderDetector"]},
                created_at=datetime.now(UTC),
            ),
        )
        assert fid is not None
        # Give the loop time to tick once.
        for _ in range(20):
            await asyncio.sleep(0.1)
            assert orch._flag_loop is not None
            if orch._flag_loop.consumed >= 1:
                break
        assert orch._flag_loop is not None
        assert orch._flag_loop.consumed >= 1
    finally:
        await orch.stop()


async def test_orchestrator_stop_is_idempotent(db: Path) -> None:
    orch = LiveOrchestrator(
        db_path=db, cfg=_minimal_config(), secrets=_minimal_secrets(),
        live_cfg=LiveConfig(
            profiles=["systematic"],
            enable_ingest=False,
            enable_inbound_bot=False,
            enable_daily_summary=False,
        ),
    )
    await orch.start()
    await orch.stop()
    # Second stop() must not raise.
    await orch.stop()
