"""72-hour dry-run script — empirical-priors addendum §9.6.

Purpose: exercise every empirical-priors code path against a compressed-time
replay so a human can ship a 72-hour interim with confidence the plumbing
holds. We do NOT exercise the LLM investigator (cost + flakiness); we
synthesize a Belief in code that mimics the investigator's output shape.

Steps:
  1. Bootstrap a tmp DB with migrations applied.
  2. Seed wallets + markets + 90 days of synthetic trades that produce
     scoring-eligible features for niche + general pools.
  3. Run discovery: features → classifier → cohort → freeze (no poly_data
     sync; uses local DB only).
  4. Open systematic + degen paper runs.
  5. Walk a synthetic Belief through the six gates twice — once passing
     (records a settlement event + closed position), once vetoed
     (records a rejection in DB + JSONL).
  6. Run `gather_experiment_data` + H1..H6 + `render_markdown`.

Exit 0 iff every step produced well-formed output. Non-zero with a
labelled failure list otherwise. Run from the repo root:

    .venv/Scripts/python.exe scripts/dry_run_72h.py

This is offline + deterministic; the "72-hour" name is intentional so
the script title matches the spec section, not a wall-clock duration.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

# Make polysim importable when run from repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from polysim.agents.belief_schema import SCHEMA_VERSION, Belief
from polysim.db import dao
from polysim.db.migrations.runner import apply_migrations
from polysim.discovery.classifier import (
    CLASSIFIER_VERSION,
    classify_population,
)
from polysim.discovery.cohort import (
    cohort_hash,
    freeze_cohort,
    select_cohort,
    update_edge_likelihoods,
)
from polysim.discovery.features import extract_features_for_run, write_features
from polysim.experiment.reporting import (
    gather_experiment_data,
    render_markdown,
    run_all_hypotheses,
)
from polysim.models import Market, TradeEvent
from polysim.portfolio.settlement import compute_settlement_window
from polysim.profiles import load_profile
from polysim.trading.decision_gate import evaluate_gates, log_rejection
from polysim.utils.time import iso

# ── helpers ─────────────────────────────────────────────


def _say(msg: str) -> None:
    print(f"[dry-run] {msg}", flush=True)


def _ok(label: str, detail: str = "") -> None:
    suffix = f" — {detail}" if detail else ""
    print(f"  PASS  {label}{suffix}", flush=True)


def _fail(label: str, detail: str) -> None:
    print(f"  FAIL  {label} — {detail}", flush=True)


# ── seeding ─────────────────────────────────────────────


async def _seed(db: Path) -> None:
    """Build a market+wallet+trade set rich enough for niche + general
    cohort selection plus a paper run."""
    await apply_migrations(db)
    base = datetime(2026, 1, 24, tzinfo=UTC)

    markets = [
        Market(
            id="m_aec_q1", slug="aec-bim-q1",
            question="Autodesk BIM-360 ARR > $700M Q1 2026?",
            category="aec",
            created_at=base - timedelta(days=120),
            resolves_at=base - timedelta(days=10),
            resolved_outcome="YES",
            resolved_at=base - timedelta(days=10),
            daily_volume_usd_cents=900_000,
        ),
        Market(
            id="m_ai_o4", slug="openai-o4-may",
            question="OpenAI ships o4 by May 2026?",
            category="ai",
            created_at=base - timedelta(days=60),
            resolves_at=base + timedelta(days=10),
            daily_volume_usd_cents=12_000_000,
        ),
        Market(
            id="m_creator_mb", slug="mrbeast-100kids-may",
            question="MrBeast '100 Kids' by May?",
            category="creator",
            created_at=base - timedelta(days=40),
            resolves_at=base + timedelta(days=14),
            daily_volume_usd_cents=2_800_000,
        ),
        Market(
            id="m_geo", slug="geo-summit-26",
            question="Maduro wins runoff?",
            category="geopolitics",
            created_at=base - timedelta(days=30),
            resolves_at=base + timedelta(days=20),
            daily_volume_usd_cents=8_500_000,
        ),
    ]
    for m in markets:
        await dao.upsert_market(db, m)

    wallets = [
        # Niche-specialist wallets — trade their niche only.
        ("0xaec_w1", "binance", "aec"),
        ("0xaec_w2", "binance", "aec"),
        ("0xai_w1", "kraken", "ai_labs"),
        ("0xai_w2", "kraken", "ai_labs"),
        ("0xcreator_w1", "binance", "creator_econ"),
        # General pool wallets.
        ("0xgen_w1", "coinbase", None),
        ("0xgen_w2", "coinbase", None),
        ("0xgen_w3", "coinbase", None),
    ]
    for addr, fund, _ in wallets:
        await dao.upsert_wallet_first_sight(db, addr)
        await dao.upsert_wallet_enrichment(
            db, address=addr, nonce=8, funding_source=fund,
            funding_first_deposit_at=iso(base - timedelta(days=300)),
        )

    # Build ≥ 60 trades per niche-specialist wallet so they pass the
    # min_niche_trades gate, plus a thinner set on the general wallets.
    trades: list[TradeEvent] = []

    def _push(addr: str, mid: str, n: int, *, win: bool) -> None:
        # `win` flips the price so the wallet is on the eventual winning
        # side (we let m_aec_q1 resolve YES, the others resolve YES too
        # for simplicity in this dry-run).
        for i in range(n):
            ts = base - timedelta(days=110 - i)
            price = 28 if win else 70
            trades.append(TradeEvent(
                id=f"{addr}_{mid}_{i}",
                wallet_address=addr,
                market_id=mid,
                side="BUY",
                outcome="YES" if win else "NO",
                size_shares=100,
                price_cents=price,
                timestamp=ts,
            ))

    for addr in ("0xaec_w1", "0xaec_w2"):
        _push(addr, "m_aec_q1", 65, win=True)
    for addr in ("0xai_w1", "0xai_w2"):
        _push(addr, "m_ai_o4", 65, win=True)
    _push("0xcreator_w1", "m_creator_mb", 60, win=True)
    for addr in ("0xgen_w1", "0xgen_w2", "0xgen_w3"):
        _push(addr, "m_geo", 30, win=True)

    await dao.insert_trades_batch(db, trades)


# ── steps ───────────────────────────────────────────────


async def step_discovery(db: Path) -> int:
    """Run features → classifier → cohort → freeze, return experiment_id."""
    features = await extract_features_for_run(db)
    if not features:
        raise AssertionError("no features extracted (seed problem)")
    await write_features(db, features)
    scores = classify_population(features)
    if not scores:
        raise AssertionError("classifier produced no scores")
    await update_edge_likelihoods(db, scores)

    features_by_key = {(f.wallet_address, f.scope): f for f in features}
    picks = select_cohort(
        scores, features_by_key,
        per_niche_target=10, general_target=10,
        min_niche_trades=50,
    )
    if not picks:
        raise AssertionError("cohort came back empty")
    expected_hash = cohort_hash(picks)
    experiment_id = await freeze_cohort(
        db,
        experiment_name="dry_run_72h",
        picks=picks,
        classifier_version=CLASSIFIER_VERSION,
        belief_schema_version=SCHEMA_VERSION,
        notes="72-hour dry run — do not pre-register.",
    )
    _ok("discovery → freeze",
        f"experiment_id={experiment_id} "
        f"size={len(picks)} hash={expected_hash[:12]}...")
    return experiment_id


async def step_paper_runs(db: Path) -> tuple[int, int]:
    """Open systematic + degen runs. Returns (sys_run_id, degen_run_id)."""
    sys_profile = load_profile("systematic")
    deg_profile = load_profile("degen")
    sys_run_id = await dao.create_paper_run(
        db, name="dry_run_systematic",
        starting_balance_cents=1_000_000,
        config_snapshot={}, profile_name=sys_profile.name,
        profile_snapshot=sys_profile.model_dump(), tag="dry_run",
    )
    deg_run_id = await dao.create_paper_run(
        db, name="dry_run_degen",
        starting_balance_cents=1_000_000,
        config_snapshot={}, profile_name=deg_profile.name,
        profile_snapshot=deg_profile.model_dump(), tag="dry_run",
    )
    _ok("paper runs opened",
        f"systematic={sys_run_id} degen={deg_run_id}")
    return sys_run_id, deg_run_id


def _build_belief(
    *,
    market_id: str,
    p_yes: float,
    confidence: float,
    resolution_risk: float,
    ev_per_contract: float,
    cohort_wallets: list[str],
) -> Belief:
    return Belief(
        market_id=market_id,
        category="event_analysis",
        confidence=confidence,
        estimated_true_probability=p_yes,
        resolution_risk_score=resolution_risk,
        expected_value_per_contract=ev_per_contract,
        rationale="dry-run synthetic belief",
        cohort_wallets_involved=cohort_wallets,
        timestamp=datetime.now(UTC),
    )


async def step_gates(db: Path, sys_run_id: int) -> None:
    """Push two synthetic Beliefs through the gates: one passes, one vetoes."""
    belief_pass = _build_belief(
        market_id="m_ai_o4",
        p_yes=0.72, confidence=0.85,
        resolution_risk=0.10, ev_per_contract=4.0,
        cohort_wallets=["0xai_w1", "0xai_w2"],
    )
    res_pass = evaluate_gates(
        belief=belief_pass, cohort_side="YES",
        spread_cost_per_contract=2.0, depth_ok=True,
        concentration_ok=True, mode="systematic",
        cycle_id="dry_run_cycle_1",
    )
    if not res_pass.overall_passed:
        raise AssertionError(
            f"expected belief_pass to pass; failed: {res_pass.reasons_summary()}"
        )

    # Record a settled position so the report has data.
    buy_ts = datetime(2026, 4, 23, 12, 0, 0, tzinfo=UTC)
    _, blocks = compute_settlement_window(buy_ts)
    if blocks < 2:
        raise AssertionError("settlement window invariant broken")
    pos_id = await dao.write_paper_position(
        db,
        run_id=sys_run_id,
        market_id="m_ai_o4",
        outcome="YES",
        size_shares=200,
        avg_entry_price_cents=40,
        source_wallet="0xai_w1",
        source_flag_id=None,
    )
    await dao.close_position(
        db, pos_id, realized_pnl_cents=600, status="CLOSED",
    )

    belief_veto = _build_belief(
        market_id="m_creator_mb",
        p_yes=0.30, confidence=0.95,
        resolution_risk=0.10, ev_per_contract=8.0,
        cohort_wallets=["0xcreator_w1"],
    )
    res_veto = evaluate_gates(
        belief=belief_veto, cohort_side="YES",
        spread_cost_per_contract=2.0, depth_ok=True,
        concentration_ok=True, mode="systematic",
        cycle_id="dry_run_cycle_2",
    )
    if res_veto.overall_passed:
        raise AssertionError("expected directional veto; got pass")
    rid = await log_rejection(
        db, result=res_veto, belief=belief_veto, flag_id=None,
    )
    if rid is None:
        raise AssertionError("rejection failed to persist")
    _ok("gates exercised",
        f"pass=1 veto=1 rejection_row={rid}")


# ── main ────────────────────────────────────────────────


async def _main() -> int:
    failures: list[tuple[str, str]] = []
    with tempfile.TemporaryDirectory(prefix="polysim_dry_run_") as tmp:
        db = Path(tmp) / "polysim.db"
        # Use a per-run rejections log so the report's gate funnel reflects
        # only this run's rejections (not whatever's in the repo's logs/).
        rejections_log = Path(tmp) / "rejections.jsonl"
        # Module-level constant is read by gather_experiment_data when no
        # explicit path is passed; we route through a local override below.

        try:
            _say("seeding DB…")
            await _seed(db)
            _ok("seed", f"db={db}")
        except Exception as exc:
            failures.append(("seed", str(exc)))
            print(json.dumps({"failures": failures}))
            return 2

        try:
            experiment_id = await step_discovery(db)
        except Exception as exc:
            failures.append(("discovery", str(exc)))
            experiment_id = 0

        try:
            sys_run_id, _ = await step_paper_runs(db)
        except Exception as exc:
            failures.append(("paper_runs", str(exc)))
            sys_run_id = 0

        if experiment_id and sys_run_id:
            # Override the rejections log path module-level so log_rejection
            # writes into the tmp dir; restore afterwards.
            from polysim.trading import decision_gate as dg
            saved_logs = dg.LOGS_DIR
            saved_path = dg.REJECTIONS_PATH
            try:
                dg.LOGS_DIR = rejections_log.parent
                dg.REJECTIONS_PATH = rejections_log
                try:
                    await step_gates(db, sys_run_id)
                except Exception as exc:
                    failures.append(("gates", str(exc)))
            finally:
                dg.LOGS_DIR = saved_logs
                dg.REJECTIONS_PATH = saved_path

            try:
                # Pull the report against the per-run rejections log.
                data = await gather_experiment_data(
                    db, experiment_id=experiment_id,
                    rejections_log=rejections_log,
                )
                results = run_all_hypotheses(data)
                if [r.hypothesis_id for r in results] != [
                    "H1", "H2", "H3", "H4", "H5", "H6"
                ]:
                    raise AssertionError("hypothesis order drifted")
                text = render_markdown(data, results)
                if "Pre-registered hypotheses" not in text:
                    raise AssertionError("missing report section")
                out = Path(tempfile.mkstemp(
                    prefix="polysim_dry_run_", suffix=".md"
                )[1])
                out.write_text(text, encoding="utf-8")
                _ok("report rendered",
                    f"runs={data.summary.paper_runs} "
                    f"closed={data.summary.paper_positions_closed} "
                    f"rejections={data.summary.rejections_total} → {out}")
            except Exception as exc:
                failures.append(("report", str(exc)))

    if failures:
        print()
        for label, detail in failures:
            _fail(label, detail)
        print()
        print(f"FAILED — {len(failures)} step(s) did not produce expected output")
        return 1
    print()
    print("OK — every empirical-priors code path produced well-formed output")
    return 0


def _force_utf8_stdout() -> None:
    """Windows consoles default to cp1252 which can't encode arrows or
    ellipses; reconfigure to UTF-8 before any user-facing prints."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")


if __name__ == "__main__":
    _force_utf8_stdout()
    raise SystemExit(asyncio.run(_main()))
