"""End-to-end user walkthrough.

Seeds a realistic DB then drives every CLI surface as a user would,
collecting pass/fail on each. Designed to be run from the repo root:

    .venv/Scripts/python.exe scripts/e2e_walkthrough.py

Exits 0 if every step passes, non-zero with a summary otherwise.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

# Make polysim importable when run from repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from polysim.db import dao
from polysim.db.migrations.runner import apply_migrations
from polysim.models import Market, TradeEvent
from polysim.profiler import wallet_profiler
from polysim.profiles import load_profile

PY = sys.executable

# ── helpers ──────────────────────────────────────────────


def _run_cli(*args: str, cwd: Path | None = None, expect_code: int | None = 0) -> tuple[int, str]:
    """Run `polysim <args>` via the repo venv. Returns (exit_code, stdout+stderr)."""
    cmd = [PY, "-m", "polysim.cli", *args]
    proc = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=60,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, out


# ── seeding ──────────────────────────────────────────────


async def seed_realistic_db(db: Path) -> dict[str, object]:
    """Seed markets + wallets + trades + flags + paper runs.

    Returns key ids (run_id, flag_id) for downstream commands.
    """
    await apply_migrations(db)
    now = datetime.now(UTC)

    # Markets across categories.
    markets = [
        Market(id="m_ai", slug="ai-1", question="Will Claude 5 ship by Q3 2026?",
               category="ai", created_at=now-timedelta(days=10),
               resolves_at=now+timedelta(days=20), daily_volume_usd_cents=4_700_000),
        Market(id="m_ai2", slug="ai-2", question="OpenAI release o4 by May?",
               category="ai", created_at=now-timedelta(days=5),
               resolves_at=now+timedelta(days=15), daily_volume_usd_cents=12_000_000),
        Market(id="m_creator", slug="cr-1", question="MrBeast '100 Kids' by May?",
               category="creator", created_at=now-timedelta(days=8),
               resolves_at=now+timedelta(days=10), daily_volume_usd_cents=2_800_000),
        Market(id="m_aec", slug="aec-1", question="Autodesk beat Q1 revenue?",
               category="aec", created_at=now-timedelta(days=12),
               resolves_at=now-timedelta(days=2), resolved_outcome="YES",
               resolved_at=now-timedelta(days=2),
               daily_volume_usd_cents=900_000),
        Market(id="m_geo", slug="geo-1", question="Maduro wins runoff?",
               category="geopolitics", created_at=now-timedelta(days=14),
               resolves_at=now+timedelta(days=8), daily_volume_usd_cents=8_500_000),
    ]
    for m in markets:
        await dao.upsert_market(db, m)

    # Wallets — one fresh insider, one mature noise wallet, one coordinator.
    wallets = [("0xinsider01", 3, "binance"), ("0xinsider02", 4, "kraken"),
               ("0xnoise03", 200, "coinbase"), ("0xcoord04", 5, "binance")]
    for addr, nonce, fund in wallets:
        await dao.upsert_wallet_first_sight(db, addr)
        await dao.upsert_wallet_enrichment(
            db, address=addr, nonce=nonce, funding_source=fund,
            funding_first_deposit_at=(now-timedelta(days=2)).isoformat(),
        )

    # Trades — background noise + insider's contrarian + coordinator follow-up.
    trades: list[TradeEvent] = []
    for mid in ("m_ai", "m_ai2", "m_creator", "m_geo"):
        for i in range(15):
            trades.append(TradeEvent(
                id=f"bg_{mid}_{i}", wallet_address=f"0xbg{i:04d}",
                market_id=mid, side="BUY", outcome="YES",
                size_shares=40, price_cents=38,
                timestamp=now-timedelta(hours=20-i),
            ))
    # Insider contrarian on m_ai.
    trades.append(TradeEvent(
        id="ins_ai", wallet_address="0xinsider01", market_id="m_ai",
        side="BUY", outcome="YES", size_shares=8000, price_cents=18,
        timestamp=now-timedelta(minutes=30),
    ))
    trades.append(TradeEvent(
        id="coord_ai", wallet_address="0xcoord04", market_id="m_ai",
        side="BUY", outcome="YES", size_shares=4000, price_cents=20,
        timestamp=now-timedelta(minutes=12),
    ))
    # Insider 2 on m_creator.
    trades.append(TradeEvent(
        id="ins_creator", wallet_address="0xinsider02", market_id="m_creator",
        side="BUY", outcome="YES", size_shares=6500, price_cents=22,
        timestamp=now-timedelta(minutes=20),
    ))
    await dao.insert_trades_batch(db, trades)

    # Refresh wallet profiles so the scorer can run.
    n = await wallet_profiler.refresh_stale_profiles(db, staleness_seconds=0, max_wallets=1000)

    # Score the insider trades to populate `flags`.
    from polysim.config import ScoringWeights
    from polysim.scoring.category_insider import CategoryInsiderDetector
    from polysim.scoring.composite import CompositeScorer, score_and_persist
    from polysim.scoring.coordination import CoordinationDetector
    from polysim.scoring.event_insider import EventInsiderDetector
    from polysim.scoring.fresh_wallet import FreshWalletDetector
    from polysim.scoring.timing import TimingDetector

    detectors = [
        CategoryInsiderDetector(min_resolved_markets=8),
        EventInsiderDetector(fresh_size_min_cents=50_000, contrarian_bps=500),
        FreshWalletDetector(),
        CoordinationDetector(db, window_hours=24),
        TimingDetector(db, late_window_hours=1),
    ]
    scorer = CompositeScorer(
        weights=ScoringWeights().model_dump(),
        flag_threshold=3.5, min_contributing_detectors=2,
    )
    flag_ids: list[int] = []
    for w, m, t in [("0xinsider01", "m_ai", "ins_ai"),
                    ("0xinsider02", "m_creator", "ins_creator")]:
        fid = await score_and_persist(
            db, scorer, detectors,
            wallet_address=w, market_id=m, trade_id=t,
        )
        if fid is not None:
            flag_ids.append(fid)

    # Spin up a paper run + drive it with the flags.
    from polysim.config import BankrollConfig
    from polysim.paper.fill_model import FillModel
    from polysim.paper.profile_executor import ProfilePaperExecutor

    profile = load_profile("systematic")
    run_id = await dao.create_paper_run(
        db, name="e2e-demo", starting_balance_cents=1_000_000,
        config_snapshot={}, profile_name=profile.name,
        profile_snapshot=profile.model_dump(), tag="e2e",
    )
    ex = ProfilePaperExecutor(db, run_id=run_id, profile=profile,
        bankroll=BankrollConfig(), fill_model=FillModel())
    for fid in flag_ids:
        await ex.consider_flag(fid)

    return {
        "run_id": run_id,
        "flag_ids": flag_ids,
        "wallets_profiled": n,
        "trades_inserted": len(trades),
        "markets_seeded": len(markets),
    }


# ── walkthrough steps ───────────────────────────────────


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="polysim_e2e_"))
    db_path = workdir / "polysim.db"
    print(f"E2E walkthrough  workdir={workdir}\n")

    # Bring the example config + .env into the workdir so polysim CLI works
    # without any external state.
    repo = Path(__file__).resolve().parent.parent
    shutil.copy(repo / "config.example.yml", workdir / "config.yml")

    # Step 1: init DB schema.
    print("[1/16] polysim init")
    code, out = _run_cli(
        "init", "--db", str(db_path), "--config", str(workdir / "config.yml"),
        cwd=workdir,
    )
    assert code == 0, f"init failed:\n{out}"

    # Step 2: seed realistic data.
    print("[2/16] seeding realistic data...")
    seed_info = asyncio.run(seed_realistic_db(db_path))
    print(f"       {seed_info}")
    run_id = int(seed_info["run_id"])  # type: ignore[arg-type]
    flag_ids = seed_info["flag_ids"]  # type: ignore[assignment]

    failures: list[str] = []

    def step(label: str, fn: Callable[[], None]) -> None:
        print(f"[{label}] ", end="", flush=True)
        try:
            fn()
            print("OK")
        except AssertionError as exc:
            print(f"FAIL — {exc}")
            failures.append(f"{label}: {exc}")

    cfg = ["--db", str(db_path), "--config", str(workdir / "config.yml")]

    # Step 3: status.
    def s_status() -> None:
        code, out = _run_cli("status", *cfg, cwd=workdir)
        assert code == 0
        assert "Database" in out
        assert "Active runs" in out
    step("3/16 polysim status", s_status)

    # Step 4: doctor.
    def s_doctor() -> None:
        code, out = _run_cli("doctor", *cfg, cwd=workdir)
        # ANTHROPIC_API_KEY missing in fresh env -> doctor flags it -> exit 1.
        # We accept exit 0 OR 1 as long as there are recognisable health lines.
        assert code in (0, 1), f"unexpected code {code}:\n{out}"
        assert "polysim doctor" in out
        assert "tables" in out
    step("4/16 polysim doctor", s_doctor)

    # Step 5: profile list.
    def s_profile_list() -> None:
        code, out = _run_cli("profile", "list", cwd=workdir)
        assert code == 0
        for n in ("systematic", "medium", "degen"):
            assert n in out
    step("5/16 polysim profile list", s_profile_list)

    # Step 6: profile show.
    def s_profile_show() -> None:
        code, out = _run_cli("profile", "show", "degen", cwd=workdir)
        assert code == 0
        assert "position_sizing_mode: percentage" in out
    step("6/16 polysim profile show degen", s_profile_show)

    # Step 7: flags list.
    def s_flags_list() -> None:
        code, out = _run_cli("flags", "list", "--since", "24h", "--db", str(db_path), cwd=workdir)
        assert code == 0
        # Either we have flags from seeding, or the table prints "(no flags ...)".
        assert "ID" in out or "no flags" in out
    step("7/16 polysim flags list", s_flags_list)

    # Step 8: flags show.
    def s_flags_show() -> None:
        if not flag_ids:
            return
        fid = int(flag_ids[0])
        code, out = _run_cli("flags", "show", str(fid), "--db", str(db_path), cwd=workdir)
        assert code == 0, out
        assert "Wallet" in out
        assert "Composite score" in out
    step("8/16 polysim flags show", s_flags_show)

    # Step 9: run list (filter by tag).
    def s_run_list() -> None:
        code, out = _run_cli("run", "list", "--tag", "e2e", "--db", str(db_path), cwd=workdir)
        assert code == 0, out
        assert f"{run_id}" in out
    step("9/16 polysim run list --tag e2e", s_run_list)

    # Step 10: run status.
    def s_run_status() -> None:
        code, out = _run_cli("run", "status", str(run_id), "--db", str(db_path), cwd=workdir)
        assert code == 0, out
        assert "starting bal" in out or "starting" in out.lower()
    step("10/16 polysim run status", s_run_status)

    # Step 11: run start-all (creates 3 more runs in a new tag).
    def s_run_start_all() -> None:
        code, out = _run_cli(
            "run", "start-all", "--tag", "e2e-multi", "--balance", "1000000",
            *cfg, cwd=workdir,
        )
        assert code == 0, out
        for p in ("systematic", "medium", "degen"):
            assert p in out
    step("11/16 polysim run start-all", s_run_start_all)

    # Step 12: run compare.
    def s_run_compare() -> None:
        code, out = _run_cli(
            "run", "compare", "--tag", "e2e-multi", "--db", str(db_path), cwd=workdir,
        )
        assert code == 0, out
        # Rich may truncate "PROFILE" to "PROF…" in narrow terminals — accept
        # either, plus row evidence (each profile name appears).
        for p in ("systematic", "medium", "degen"):
            # rich may truncate "systematic" → "syst…" too, so use a prefix.
            assert p[:4] in out, f"profile {p} not present in compare output:\n{out}"
    step("12/16 polysim run compare --tag", s_run_compare)

    # Step 13: dashboard --pop-flag (one-shot detail render).
    def s_dashboard_pop() -> None:
        if not flag_ids:
            return
        code, out = _run_cli(
            "dashboard", "--pop-flag", str(int(flag_ids[0])),
            "--db", str(db_path), cwd=workdir,
        )
        assert code == 0, out
        assert "Wallet" in out
    step("13/16 polysim dashboard --pop-flag", s_dashboard_pop)

    # Step 14: report --tag (comparative).
    def s_report_tag() -> None:
        out_path = workdir / "report-e2e.md"
        code, out = _run_cli(
            "report", "--tag", "e2e", "--out", str(out_path),
            "--db", str(db_path), cwd=workdir,
        )
        assert code == 0, out
        body = out_path.read_text(encoding="utf-8")
        assert "Headline" in body
        assert "Bootstrap" in body
    step("14/16 polysim report --tag", s_report_tag)

    # Step 15: replay (offline scoring).
    def s_replay() -> None:
        # Just span 4 days around our seed window.
        from_d = (datetime.now(UTC) - timedelta(days=2)).date().isoformat()
        to_d = (datetime.now(UTC) + timedelta(days=2)).date().isoformat()
        code, out = _run_cli(
            "replay", "--from", from_d, "--to", to_d, "--no-run",
            "--profile", "systematic",
            *cfg, cwd=workdir,
        )
        assert code == 0, out
        assert "trades=" in out
    step("15/16 polysim replay", s_replay)

    # Step 16: web dashboard — boot in-process, hit endpoints, shut down.
    def s_web() -> None:
        from polysim.web.server import run_server_in_thread
        srv, _ = run_server_in_thread(db_path, host="127.0.0.1", port=18790)
        try:
            time.sleep(0.3)
            health = urllib.request.urlopen(
                "http://127.0.0.1:18790/api/health", timeout=2,
            ).read()
            assert b"ok" in health
            html = urllib.request.urlopen(
                "http://127.0.0.1:18790/", timeout=2,
            ).read()
            assert b"PolySim" in html
            state = urllib.request.urlopen(
                "http://127.0.0.1:18790/api/state", timeout=2,
            ).read()
            assert b"runs" in state
        finally:
            srv.shutdown()
    step("16/16 polysim web (in-process)", s_web)

    # Final report.
    print()
    if failures:
        print(f"FAIL — {len(failures)} step(s) failed:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS — every step OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
