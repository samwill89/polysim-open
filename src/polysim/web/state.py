"""Async state collector for the web dashboard.

Returns a JSON-serializable dict that the SPA renders into the five
panels. One snapshot per poll — keep it cheap, every query bounded.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

from polysim.db import dao
from polysim.portfolio.valuation import (
    best_bid_ask_from_snapshot,
    value_position,
)
from polysim.utils.time import iso, now_utc

EQUITY_TAG = "equity_v1"


async def collect_dashboard_state(
    db_path: Path,
    *,
    flag_limit: int = 25,
    trade_limit: int = 25,
    selected_flag_id: int | None = None,
    selected_run_id: int | None = None,
) -> dict[str, Any]:
    """One snapshot. Always returns a dict — never raises into the handler."""
    state: dict[str, Any] = {
        "now_utc": now_utc().isoformat(),
        "stats": await dao.db_stats(db_path),
        "flags": [],
        "trades": [],
        "runs": [],
        "selected_flag": None,
        "selected_run": None,
        "telegram": [],
        "open_positions": [],
        "pnl_by_category": {},
        "unrealized_pnl_by_category": {},
        "drawdown_pct": 0.0,
        "drawdown_limit_pct": 0.20,
        "flags_last_hour": 0,
        "max_flags_per_hour": 100,
        "llm_calls_today": 0,
        "llm_max_per_day": 100,
        "last_trade_at": None,
        "trades_last_1h": 0,
        "signals": [],
        "risk_intelligence": {
            "decisions": [],
            "assessments": [],
            "counts_24h": {"passed": 0, "blocked": 0},
        },
    }

    # Recent conversation signals (signals package) — empty list until the
    # layer is enabled and produces rows; never fails the snapshot.
    try:
        from polysim.signals.service import recent_market_signals

        state["signals"] = await recent_market_signals(db_path, limit=12)
    except Exception:
        state["signals"] = []

    # Evidence/correlation/edge decisions. These are execution controls, not
    # attention signals, so keep a separate operator-visible surface.
    try:
        from polysim.risk_intelligence.service import (
            recent_evidence_assessments,
            recent_risk_decisions,
            risk_decision_counts_24h,
        )

        state["risk_intelligence"] = {
            "decisions": await recent_risk_decisions(db_path, limit=12),
            "assessments": await recent_evidence_assessments(db_path, limit=8),
            "counts_24h": await risk_decision_counts_24h(db_path),
        }
    except Exception:
        pass

    # Recent flags (newest first).
    state["flags"] = await dao.list_flags_since(
        db_path,
        since_iso=iso(now_utc() - timedelta(hours=72)),
        category=None,
        limit=flag_limit,
    )

    # Recent trades.
    state["trades"] = await _recent_trades(db_path, limit=trade_limit)

    # Prediction-market runs. Equity has its own section, and every active
    # tournament control stays visible even when it is older than recent runs.
    recent_runs = await dao.list_paper_runs(db_path, limit=50)
    tournament_runs = await dao.list_paper_runs_by_tag(db_path, tag="tournament_v1")
    active_tournament_runs = [
        r for r in tournament_runs if r.get("ended_at") is None and r.get("paused_at") is None
    ]
    runs = [r for r in recent_runs if not _is_equity_run(r)][:15]
    known_run_ids = {int(r["id"]) for r in runs}
    runs.extend(r for r in active_tournament_runs if int(r["id"]) not in known_run_ids)
    runs.sort(key=lambda r: str(r.get("started_at") or ""), reverse=True)
    state["runs"] = [await _serialize_run_with_valuation(db_path, r) for r in runs]

    # Effective run id — default to the newest live prediction-market run.
    # Equity has its own dashboard section and must not displace this panel.
    eff_run_id = selected_run_id
    if eff_run_id is None:
        for r in reversed(active_tournament_runs):
            eff_run_id = int(r["id"])
            break
        if eff_run_id is None:
            for r in runs:
                if (
                    r.get("ended_at") is None
                    and r.get("paused_at") is None
                    and r.get("tag") != EQUITY_TAG
                ):
                    eff_run_id = int(r["id"])
                    break
        if eff_run_id is None and runs:
            eff_run_id = int(runs[0]["id"])

    if eff_run_id is not None:
        run = await dao.get_paper_run(db_path, eff_run_id)
        if run is not None:
            opens = await _open_positions_with_question(db_path, eff_run_id)
            state["selected_run"] = await _serialize_run_with_valuation(
                db_path,
                run,
                open_positions=opens,
            )
            state["open_positions"] = [_serialize_position(p) for p in opens]
            # Drawdown.
            start = int(run.get("starting_balance_cents") or 0)
            cur = int(state["selected_run"].get("account_value_cents") or 0)
            if start > 0:
                state["drawdown_pct"] = max(0.0, (start - cur) / start)
            # P&L by category — closed only.
            state["pnl_by_category"] = await _pnl_by_category(db_path, eff_run_id)
            state["unrealized_pnl_by_category"] = (
                state["selected_run"].get("unrealized_pnl_by_category") or {}
            )

    # Selected flag detail (with components decoded).
    if selected_flag_id is not None:
        row = await dao.get_flag(db_path, selected_flag_id)
        if row is not None:
            state["selected_flag"] = _serialize_flag(row)
    elif state["flags"]:
        # Default to the newest flag so the panel is never empty.
        latest_id = int(state["flags"][0]["id"])
        row = await dao.get_flag(db_path, latest_id)
        if row is not None:
            state["selected_flag"] = _serialize_flag(row)

    # Heartbeat panel inputs.
    from polysim.utils.watchdog import latest_trade_timestamp

    last_at = await latest_trade_timestamp(db_path)
    state["last_trade_at"] = last_at.isoformat() if last_at else None
    state["trades_last_1h"] = await _trades_since(db_path, iso(now_utc() - timedelta(hours=1)))
    state["flags_last_hour"] = await dao.count_flags_since(
        db_path, since_iso=iso(now_utc() - timedelta(hours=1))
    )
    cost = await dao.sum_flag_costs_since(db_path, since_iso=iso(now_utc() - timedelta(hours=24)))
    state["llm_calls_today"] = int(cost.get("total_calls", 0))
    state["llm_cost_cents_today"] = int(cost.get("total_cost_cents", 0))

    # Synthetic telegram timeline — derived from recent flags + position events
    # so the operator gets the same cadence in both surfaces.
    state["telegram"] = await _telegram_timeline(
        db_path, eff_run_id, recent_flag_rows=state["flags"][:6]
    )

    # Comparative / multi-run summary.
    if active_tournament_runs:
        state["active_runs_summary"] = [
            _summarize_run(await _serialize_run_with_valuation(db_path, run))
            for run in active_tournament_runs
        ]
    else:
        state["active_runs_summary"] = [
            _summarize_run(r)
            for r in state["runs"]
            if (
                r.get("ended_at") is None
                and r.get("paused_at") is None
                and r.get("tag") != EQUITY_TAG
            )
        ]

    # Equity sentiment track (parallel lane).
    state["equity"] = await _equity_summary(db_path)

    return state


async def _equity_summary(db_path: Path) -> dict[str, Any]:
    """Variant runs + top signals + recent open positions for the equity tab."""
    if not db_path.exists():
        return {"runs": [], "signals": [], "positions": []}
    import aiosqlite

    out: dict[str, Any] = {"runs": [], "signals": [], "positions": []}
    try:
        async with aiosqlite.connect(str(db_path)) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT id, name, starting_balance_cents, current_balance_cents "
                "FROM paper_runs WHERE tag = 'equity_v1' AND ended_at IS NULL "
                "ORDER BY current_balance_cents DESC"
            ) as cur:
                for r in await cur.fetchall():
                    start = int(r["starting_balance_cents"] or 0)
                    bal = int(r["current_balance_cents"] or 0)
                    async with db.execute(
                        "SELECT COUNT(*) FROM equity_positions "
                        "WHERE run_id = ? AND status = 'OPEN'",
                        (r["id"],),
                    ) as pc:
                        prow = await pc.fetchone()
                        npos = int(prow[0]) if prow else 0
                    out["runs"].append(
                        {
                            "id": int(r["id"]),
                            "name": str(r["name"]).replace("equity-", ""),
                            "balance_cents": bal,
                            "pnl_pct": (bal - start) / start if start else 0.0,
                            "open_positions": npos,
                        }
                    )
            async with db.execute(
                "SELECT ticker, composite, z_attn, stance, mentions FROM equity_signals "
                "WHERE date = (SELECT MAX(date) FROM equity_signals) "
                "ORDER BY composite DESC LIMIT 10"
            ) as cur:
                out["signals"] = [dict(r) for r in await cur.fetchall()]
            async with db.execute(
                "SELECT p.ticker, p.shares, p.avg_entry_price_cents, r.name "
                "FROM equity_positions p JOIN paper_runs r ON r.id = p.run_id "
                "WHERE p.status = 'OPEN' AND r.tag = 'equity_v1' "
                "AND r.ended_at IS NULL "
                "ORDER BY p.opened_at DESC LIMIT 12"
            ) as cur:
                out["positions"] = [
                    {
                        "ticker": r["ticker"],
                        "shares": round(float(r["shares"]), 1),
                        "entry_cents": int(r["avg_entry_price_cents"]),
                        "run": str(r["name"]).replace("equity-", ""),
                    }
                    for r in await cur.fetchall()
                ]
    except aiosqlite.OperationalError:
        return out
    return out


# ── helpers ──────────────────────────────────────────────


async def _serialize_run_with_valuation(
    db_path: Path,
    r: dict[str, Any],
    *,
    open_positions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    valuation = await _run_valuation(
        db_path,
        r,
        open_positions=open_positions,
    )
    return _serialize_run(r, valuation=valuation)


def _serialize_run(
    r: dict[str, Any],
    *,
    valuation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    start = int(r.get("starting_balance_cents") or 0)
    cur = int(r.get("current_balance_cents") or 0)
    account_value = int((valuation or {}).get("account_value_cents", cur) or 0)
    pnl = account_value - start
    pnl_pct = (pnl / start) if start else 0.0
    return {
        "id": int(r["id"]),
        "name": r.get("name"),
        "profile_name": r.get("profile_name"),
        "tag": r.get("tag"),
        "started_at": r.get("started_at"),
        "ended_at": r.get("ended_at"),
        "paused_at": r.get("paused_at"),
        "pause_reason": r.get("pause_reason"),
        "starting_balance_cents": start,
        "current_balance_cents": cur,
        "cash_balance_cents": None if _is_equity_run(r) else cur,
        "account_value_cents": account_value,
        "open_position_cost_cents": int((valuation or {}).get("open_position_cost_cents", 0) or 0),
        "open_position_value_cents": int(
            (valuation or {}).get("open_position_value_cents", 0) or 0
        ),
        "unrealized_pnl_cents": int((valuation or {}).get("unrealized_pnl_cents", 0) or 0),
        "unrealized_pnl_by_category": (
            (valuation or {}).get("unrealized_pnl_by_category", {}) or {}
        ),
        "mark_to_market": (valuation or {}).get("mark_to_market", {}),
        "balance_semantics": ("total_equity" if _is_equity_run(r) else "cash_plus_open_positions"),
        "pnl_cents": pnl,
        "pnl_pct": pnl_pct,
    }


def _serialize_position(p: dict[str, Any]) -> dict[str, Any]:
    size = int(p.get("size_shares") or 0)
    entry = int(p.get("avg_entry_price_cents") or 0)
    mark_price = p.get("_mark_price_cents")
    mark_value = p.get("_mark_value_cents")
    unrealized = p.get("_unrealized_pnl_cents")
    return {
        "id": int(p["id"]),
        "market_id": p.get("market_id"),
        "market_question": p.get("question"),
        "category": p.get("category"),
        "resolved_outcome": p.get("resolved_outcome"),
        "outcome": p.get("outcome"),
        "size_shares": size,
        "avg_entry_price_cents": entry,
        "notional_cents": size * entry,
        "mark_price_cents": int(mark_price) if mark_price is not None else None,
        "mark_source": p.get("_mark_source"),
        "mark_value_cents": int(mark_value) if mark_value is not None else None,
        "unrealized_pnl_cents": (int(unrealized) if unrealized is not None else None),
        "opened_at": p.get("opened_at"),
        "source_wallet": p.get("source_wallet"),
        "source_flag_id": p.get("source_flag_id"),
        "status": p.get("status"),
    }


def _is_equity_run(r: dict[str, Any]) -> bool:
    return str(r.get("tag") or "") == EQUITY_TAG or str(r.get("name") or "").startswith("equity-")


async def _run_valuation(
    db_path: Path,
    run: dict[str, Any],
    *,
    open_positions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Value a run for display without changing stored run balances."""
    cur = int(run.get("current_balance_cents") or 0)
    if _is_equity_run(run):
        return {
            "account_value_cents": cur,
            "open_position_cost_cents": 0,
            "open_position_value_cents": 0,
            "unrealized_pnl_cents": 0,
            "unrealized_pnl_by_category": {},
            "mark_to_market": {
                "source": "equity_close",
                "bid_marked_positions": 0,
                "entry_fallback_positions": 0,
                "resolved_positions": 0,
            },
        }

    positions = open_positions
    if positions is None:
        positions = await dao.list_open_positions(db_path, int(run.get("id") or 0))

    open_cost = 0
    open_value = 0
    unrealized_by_category: dict[str, int] = {}
    bid_count = 0
    fallback_count = 0
    resolved_count = 0

    for pos in positions:
        size = int(pos.get("size_shares") or 0)
        entry = int(pos.get("avg_entry_price_cents") or 0)
        cost = size * entry
        open_cost += cost
        market_id = str(pos.get("market_id") or "")
        outcome = str(pos.get("outcome") or "")
        resolved_outcome = pos.get("resolved_outcome")

        mark_price = entry
        mark_source = "entry_fallback"
        if resolved_outcome in {"YES", "NO", "INVALID"}:
            mark_price = 100 if resolved_outcome == outcome else 0
            mark_source = "resolution"
            resolved_count += 1
        elif market_id and outcome:
            quote = await best_bid_ask_from_snapshot(
                db_path,
                market_id=market_id,
                outcome=outcome,
            )
            if quote is not None:
                best_bid, best_ask = quote
                bid_val = value_position(
                    position_id=int(pos.get("id") or 0),
                    size_shares=size,
                    avg_entry_price_cents=entry,
                    bid_price_cents=best_bid,
                    ask_price_cents=best_ask,
                )
                mark_price = bid_val.bid_price_cents
                mark_source = "bid"
                bid_count += 1
            else:
                fallback_count += 1
        else:
            fallback_count += 1

        val = value_position(
            position_id=int(pos.get("id") or 0),
            size_shares=size,
            avg_entry_price_cents=entry,
            bid_price_cents=mark_price,
            ask_price_cents=mark_price,
        )
        open_value += val.value_cents
        cat = str(pos.get("category") or "unknown")
        unrealized_by_category[cat] = unrealized_by_category.get(cat, 0) + val.unrealized_pnl_cents
        pos["_mark_price_cents"] = mark_price
        pos["_mark_source"] = mark_source
        pos["_mark_value_cents"] = val.value_cents
        pos["_unrealized_pnl_cents"] = val.unrealized_pnl_cents

    return {
        "account_value_cents": cur + open_value,
        "open_position_cost_cents": open_cost,
        "open_position_value_cents": open_value,
        "unrealized_pnl_cents": open_value - open_cost,
        "unrealized_pnl_by_category": unrealized_by_category,
        "mark_to_market": {
            "source": "bid_or_entry_fallback",
            "bid_marked_positions": bid_count,
            "entry_fallback_positions": fallback_count,
            "resolved_positions": resolved_count,
        },
    }


async def _open_positions_with_question(
    db_path: Path,
    run_id: int,
) -> list[dict[str, Any]]:
    """Like dao.list_open_positions but LEFT JOINs market question/category."""
    if not db_path.exists():
        return []
    import aiosqlite

    try:
        async with aiosqlite.connect(str(db_path)) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT p.*, m.question, m.category, m.resolved_outcome
                FROM paper_positions p
                LEFT JOIN markets m ON m.id = p.market_id
                WHERE p.run_id = ? AND p.status = 'OPEN'
                ORDER BY p.opened_at
                """,
                (run_id,),
            ) as cur:
                rows = await cur.fetchall()
    except aiosqlite.OperationalError:
        return []
    return [dict(r) for r in rows]


def _summarize_run(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": int(r["id"]),
        "name": r.get("name"),
        "profile_name": r.get("profile_name"),
        "balance_cents": int(r.get("account_value_cents") or r.get("current_balance_cents") or 0),
        "pnl_pct": float(r.get("pnl_pct") or 0.0),
        "paused": bool(r.get("paused_at")),
    }


def _serialize_flag(row: dict[str, Any]) -> dict[str, Any]:
    components_raw = row.get("components_json") or "{}"
    try:
        components = (
            json.loads(components_raw) if isinstance(components_raw, str) else components_raw
        )
    except json.JSONDecodeError:
        components = {}
    return {
        "id": int(row["id"]),
        "wallet_address": row.get("wallet_address"),
        "market_id": row.get("market_id"),
        "market_question": row.get("question"),
        "category": row.get("category"),
        "daily_volume_usd_cents": row.get("daily_volume_usd_cents"),
        "detector_name": row.get("detector_name"),
        "raw_score": row.get("raw_score"),
        "composite_score": row.get("composite_score"),
        "investigator_verdict": row.get("investigator_verdict"),
        "investigator_reasoning": row.get("investigator_reasoning"),
        "components": components,
        "acted_on": bool(row.get("acted_on")),
        "created_at": row.get("created_at"),
        "wallet": {
            "first_seen_at": row.get("first_seen_at"),
            "nonce": row.get("nonce"),
            "funding_source": row.get("funding_source"),
            "funding_first_deposit_at": row.get("funding_first_deposit_at"),
            "lifetime_volume_cents": int(row.get("lifetime_volume_cents") or 0),
            "lifetime_trades": int(row.get("lifetime_trades") or 0),
            "owner_address": row.get("owner_address"),
        },
    }


async def _recent_trades(db_path: Path, *, limit: int) -> list[dict[str, Any]]:
    if not db_path.exists():
        return []
    import aiosqlite

    try:
        async with aiosqlite.connect(str(db_path)) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT t.id, t.wallet_address, t.market_id, t.side, t.outcome,
                       t.size_shares, t.price_cents, t.timestamp,
                       m.question, m.category
                FROM trades t LEFT JOIN markets m ON m.id = t.market_id
                ORDER BY t.timestamp DESC LIMIT ?
                """,
                (limit,),
            ) as cur:
                rows = await cur.fetchall()
    except aiosqlite.OperationalError:
        return []
    return [
        {
            "id": r["id"],
            "wallet_address": r["wallet_address"],
            "market_id": r["market_id"],
            "side": r["side"],
            "outcome": r["outcome"],
            "size_shares": int(r["size_shares"]),
            "price_cents": int(r["price_cents"]),
            "notional_cents": int(r["size_shares"]) * int(r["price_cents"]),
            "timestamp": r["timestamp"],
            "market_question": r["question"],
            "category": r["category"],
        }
        for r in rows
    ]


async def _trades_since(db_path: Path, since_iso: str) -> int:
    if not db_path.exists():
        return 0
    import aiosqlite

    try:
        async with (
            aiosqlite.connect(str(db_path)) as db,
            db.execute(
                "SELECT COUNT(*) FROM trades WHERE timestamp >= ?",
                (since_iso,),
            ) as cur,
        ):
            row = await cur.fetchone()
    except aiosqlite.OperationalError:
        return 0
    return int(row[0]) if row and row[0] is not None else 0


async def _pnl_by_category(db_path: Path, run_id: int) -> dict[str, int]:
    import aiosqlite

    out: dict[str, int] = {}
    if not db_path.exists():
        return out
    try:
        async with (
            aiosqlite.connect(str(db_path)) as db,
            db.execute(
                "SELECT m.category, COALESCE(SUM(p.realized_pnl_cents), 0) "
                "FROM paper_positions p LEFT JOIN markets m ON m.id = p.market_id "
                "WHERE p.run_id = ? AND p.status IN ('CLOSED','RESOLVED') "
                "GROUP BY m.category",
                (run_id,),
            ) as cur,
        ):
            rows = await cur.fetchall()
    except aiosqlite.OperationalError:
        return out
    for r in rows:
        cat = str(r[0] or "unknown")
        out[cat] = int(r[1] or 0)
    return out


async def _telegram_timeline(
    db_path: Path,
    run_id: int | None,
    *,
    recent_flag_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Synthesize a telegram-style timeline from recent flags + position events.

    The real Telegram sink is push-only and we don't persist sent messages
    to the DB — this view reconstructs an equivalent timeline so the
    operator sees the same chronology in both surfaces.
    """
    timeline: list[dict[str, Any]] = []
    for f in recent_flag_rows:
        timeline.append(
            {
                "kind": "flag",
                "ts": f.get("created_at"),
                "title": f"Flag #{f.get('id')}  composite {float(f.get('composite_score') or 0):.1f}  "
                f"{f.get('investigator_verdict') or 'unchecked'}",
                "lines": [
                    f"Market: {f.get('question') or f.get('market_id')}",
                    f"Wallet: {(f.get('wallet_address') or '')[:10]}...",
                    f"Category: {f.get('category') or '-'}",
                ],
            }
        )
    if run_id is not None:
        # Recent fills for this run -> position-opened messages.
        fills = await dao.list_paper_fills(db_path, run_id)
        for fill in fills[-5:][::-1]:
            cents = int(fill.get("size_shares") or 0) * int(fill.get("fill_price_cents") or 0)
            timeline.append(
                {
                    "kind": "position_opened" if fill.get("side") == "BUY" else "position_closed",
                    "ts": fill.get("timestamp"),
                    "title": ("Position opened" if fill.get("side") == "BUY" else "Position closed")
                    + f"  · run #{run_id}",
                    "lines": [
                        f"{fill.get('size_shares')} @ {fill.get('fill_price_cents')}c "
                        f"(${cents / 100:,.2f})",
                        f"slippage {fill.get('slippage_cents')}c · "
                        f"latency {fill.get('latency_ms')}ms",
                    ],
                }
            )
    timeline.sort(key=lambda x: str(x.get("ts") or ""), reverse=True)
    return timeline[:12]
