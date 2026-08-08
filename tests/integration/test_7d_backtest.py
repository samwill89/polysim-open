"""7-day backtest end-to-end — build plan §4.13.

Synthesizes 7 days of flagged trades across 3 markets, feeds them through
the paper executor with the configured fill model, then closes positions
on resolution. Asserts:
  * run balance never goes negative at any step
  * no position exceeds the configured caps
  * all kill switches can be tripped on demand
  * a full trade log is produced (paper_fills rows for every position)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from polysim.config import BankrollConfig, Config, RunConfig
from polysim.db import dao
from polysim.db.migrations.runner import apply_migrations
from polysim.models import Flag, Market, TradeEvent
from polysim.paper import kill_switches, run_manager

# Anchor the synthetic week relative to *now*: process_pending_flags scans a
# rolling 30-day window, so a fixed calendar date becomes a time-bomb that
# starts failing 30 days after it's written (it did — 2026-04-01 originally).
_BASE = datetime.now(UTC) - timedelta(days=10)


def _cfg() -> Config:
    return Config(
        run=RunConfig(name="7d-synth", mode="backtest", starting_balance_cents=1_000_000),
        bankroll=BankrollConfig(
            max_open_positions=20,
            max_pct_per_position=0.05,
            max_pct_per_source_wallet=0.20,
            max_pct_per_market=0.10,
            fixed_copy_cents=5_000,
            kill_drawdown_pct=0.20,
            kill_flags_per_hour=10_000,  # don't trip in normal flow
        ),
        categories={},
    )


async def _seed(db: Path) -> list[Flag]:
    """Build 3 markets + 3 flags + 3 triggering trades over 7 days."""
    base = _BASE
    # Mix of outcomes so the run closes with a bit of each.
    markets = [
        ("m_ai",   "ai",    "YES"),
        ("m_aec",  "aec",   "NO"),
        ("m_geo",  "geopolitics", "YES"),
    ]
    # Create markets UNRESOLVED so the executor opens into them.
    for mid, cat, _outcome in markets:
        await dao.upsert_market(db, Market(
            id=mid, slug=mid, question=f"Q-{mid}?",
            category=cat,  # type: ignore[arg-type]
            created_at=base,
            resolves_at=base + timedelta(days=7),
            resolved_outcome=None,
            resolved_at=None,
            daily_volume_usd_cents=50_000_00,
        ))

    await dao.upsert_wallet_first_sight(db, "0xinsider")
    await dao.upsert_wallet_enrichment(
        db, address="0xinsider", nonce=3,
        funding_source="binance", funding_first_deposit_at=None,
    )

    flags: list[Flag] = []
    for i, (mid, _cat, outcome) in enumerate(markets):
        trade_id = f"t_{i}"
        await dao.insert_trades_batch(db, [TradeEvent(
            id=trade_id,
            wallet_address="0xinsider",
            market_id=mid,
            side="BUY",
            outcome=outcome,  # type: ignore[arg-type]
            size_shares=1_000,
            price_cents=32 + i,
            timestamp=base + timedelta(days=i + 1),
        )])
        flag_id = await dao.write_flag(db, Flag(
            wallet_address="0xinsider",
            market_id=mid,
            trade_id=trade_id,
            detector_name="composite",
            raw_score=6.0,
            composite_score=7.5,
            components={},
            created_at=base + timedelta(days=i + 1, minutes=1),
        ))
        assert flag_id is not None
        # Rehydrate for test assertions.
        stored = await dao.get_flag(db, flag_id)
        assert stored is not None
        flags.append(Flag(
            id=stored["id"],
            wallet_address=stored["wallet_address"],
            market_id=stored["market_id"],
            trade_id=stored.get("trade_id"),
            detector_name=stored["detector_name"],
            raw_score=stored["raw_score"],
            composite_score=stored.get("composite_score"),
            components={},
            created_at=datetime.fromisoformat(str(stored["created_at"]).replace("Z", "")).replace(tzinfo=UTC),
        ))
    return flags


@pytest.mark.integration
async def test_7d_backtest_happy_path(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    await apply_migrations(db)
    cfg = _cfg()
    flags = await _seed(db)

    run_id = await run_manager.start_run(db, cfg, reports_dir=tmp_path / "reports")

    # Process all seeded flags.
    opened = await run_manager.process_pending_flags(
        db, cfg, run_id=run_id, since_seconds=30 * 24 * 3600, limit=500,
    )
    assert opened == len(flags), (
        f"expected {len(flags)} positions opened, got {opened}"
    )

    balance_after_open = await dao.get_paper_run(db, run_id)
    assert balance_after_open is not None
    assert balance_after_open["current_balance_cents"] >= 0, "balance went negative"

    # Now resolve each market — mirrors end-of-window in a real backtest.
    base = _BASE
    outcomes = {"m_ai": "YES", "m_aec": "NO", "m_geo": "YES"}
    for mid, outcome in outcomes.items():
        await dao.upsert_market(db, Market(
            id=mid, slug=mid, question=f"Q-{mid}?",
            category="ai",  # type: ignore[arg-type]
            created_at=base,
            resolves_at=base + timedelta(days=7),
            resolved_outcome=outcome,  # type: ignore[arg-type]
            resolved_at=base + timedelta(days=7, hours=1),
            daily_volume_usd_cents=50_000_00,
        ))

    # Close resolved markets.
    closed = await run_manager.resolve_closed_markets(db, cfg, run_id=run_id)
    assert closed == len(flags)

    status = await run_manager.run_status(db, run_id)
    # After resolution the run balance must be non-negative.
    assert status["current_balance_cents"] >= 0
    # Full trade log: one fill per position.
    fills = await dao.list_paper_fills(db, run_id)
    assert len(fills) >= len(flags)
    # No position exceeded the per-position cap.
    max_allowed = cfg.run.starting_balance_cents * cfg.bankroll.max_pct_per_position
    for f in fills:
        notional = int(f["size_shares"]) * int(f["fill_price_cents"])
        assert notional <= max_allowed * 1.01, (
            f"position notional {notional} exceeded per-position cap {max_allowed}"
        )


@pytest.mark.integration
async def test_drawdown_kill_switch_trips_and_pauses_run(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    await apply_migrations(db)
    cfg = _cfg()
    run_id = await run_manager.start_run(db, cfg, reports_dir=tmp_path / "reports")
    # Synthetic 25% drawdown.
    await dao.adjust_run_balance(db, run_id, -250_000)
    reason = await kill_switches.check_and_pause(
        db, run_id,
        max_drawdown_pct=cfg.bankroll.kill_drawdown_pct,
        max_flags_per_hour=cfg.bankroll.kill_flags_per_hour,
    )
    assert reason is not None and reason.code == "drawdown"
    status = await run_manager.run_status(db, run_id)
    assert status["paused"] is True


@pytest.mark.integration
async def test_flag_rate_kill_switch_trips(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    await apply_migrations(db)
    cfg = _cfg()
    # Lower the threshold so we can trip with fewer seed flags.
    cfg_tight = cfg.model_copy(
        update={
            "bankroll": cfg.bankroll.model_copy(
                update={"kill_flags_per_hour": 5}
            )
        }
    )
    run_id = await run_manager.start_run(db, cfg_tight, reports_dir=tmp_path / "reports")

    # Seed 10 flags in the last hour across 10 distinct markets.
    await dao.upsert_wallet_first_sight(db, "0xaf4")
    for i in range(10):
        mid = f"m{i}"
        await dao.upsert_market(db, Market(
            id=mid, slug=mid, question="Q?",
            created_at=datetime(2026, 4, 1, tzinfo=UTC),
        ))
        await dao.write_flag(db, Flag(
            wallet_address="0xaf4", market_id=mid,
            detector_name="composite",
            raw_score=6.0, composite_score=8.0,
            components={},
            created_at=datetime.now(UTC) - timedelta(minutes=i),
        ))

    reason = await kill_switches.check_and_pause(
        db, run_id,
        max_drawdown_pct=cfg_tight.bankroll.kill_drawdown_pct,
        max_flags_per_hour=cfg_tight.bankroll.kill_flags_per_hour,
    )
    assert reason is not None and reason.code == "flag_rate"
