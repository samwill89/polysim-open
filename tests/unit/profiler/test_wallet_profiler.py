"""Wallet profiler tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from polysim.models import Market, TradeEvent, WalletProfile
from polysim.profiler import wallet_profiler
from polysim.utils.time import now_utc


def _market(
    id_: str,
    *,
    category: str | None = "ai",
    resolved_outcome: str | None = "YES",
    resolved_at: datetime | None = None,
) -> Market:
    created = datetime(2026, 4, 1, tzinfo=UTC)
    return Market(
        id=id_,
        slug=f"slug-{id_}",
        question=f"Q {id_}?",
        category=category,  # type: ignore[arg-type]
        created_at=created,
        resolves_at=resolved_at or (created + timedelta(days=30)),
        resolved_outcome=resolved_outcome,  # type: ignore[arg-type]
        resolved_at=resolved_at or (created + timedelta(days=30)),
        daily_volume_usd_cents=100_000_00,
    )


def _trade(
    id_: str,
    market_id: str,
    *,
    side: str = "BUY",
    outcome: str = "YES",
    size: int = 100,
    price: int = 40,
    ts: datetime | None = None,
) -> TradeEvent:
    return TradeEvent(
        id=id_,
        wallet_address="0xaf4",
        market_id=market_id,
        side=side,  # type: ignore[arg-type]
        outcome=outcome,  # type: ignore[arg-type]
        size_shares=size,
        price_cents=price,
        timestamp=ts or datetime(2026, 4, 5, tzinfo=UTC),
    )


def test_empty_history_returns_zero_profile() -> None:
    p = wallet_profiler.compute("0xaf4", [])
    assert p.total_markets == 0
    assert p.resolved_markets == 0
    assert p.wins == 0
    assert p.losses == 0
    assert p.win_rate == 0.0


def test_single_winning_trade() -> None:
    m = _market("m1")
    t = _trade("t1", "m1", side="BUY", outcome="YES", price=40, size=100)
    p = wallet_profiler.compute("0xaf4", [(t, m)])
    assert p.wins == 1
    assert p.losses == 0
    assert p.win_rate == 1.0
    # 100 * (100 - 40) = 6000 cents
    assert p.total_pnl_cents == 6000


def test_single_losing_trade() -> None:
    m = _market("m1", resolved_outcome="NO")
    t = _trade("t1", "m1", side="BUY", outcome="YES", price=40, size=100)
    p = wallet_profiler.compute("0xaf4", [(t, m)])
    assert p.wins == 0 and p.losses == 1
    assert p.total_pnl_cents == -4000  # -100 * 40


def test_unresolved_trade_ignored_for_wl() -> None:
    m = _market("m1", resolved_outcome=None)
    t = _trade("t1", "m1", side="BUY", outcome="YES")
    p = wallet_profiler.compute("0xaf4", [(t, m)])
    assert p.wins == 0 and p.losses == 0
    assert p.total_markets == 1
    assert p.resolved_markets == 0


def test_invalid_market_pnl_zero() -> None:
    m = _market("m1", resolved_outcome="INVALID")
    t = _trade("t1", "m1", size=100, price=40)
    p = wallet_profiler.compute("0xaf4", [(t, m)])
    # Per spec §9: invalid → pnl=0
    assert p.total_pnl_cents == 0


def test_category_exclusivity_herfindahl() -> None:
    """Single-category wallet → H = 1.0. Evenly split 2 cats → H = 0.5."""
    m_ai = _market("m1", category="ai")
    m_ai2 = _market("m2", category="ai")
    m_aec = _market("m3", category="aec")
    m_aec2 = _market("m4", category="aec")

    # 4 ai, 0 other → H = (4/4)^2 = 1
    trades_1 = [(_trade(f"t{i}", "m1"), m_ai) for i in range(4)]
    p1 = wallet_profiler.compute("0xaf4", trades_1)
    assert p1.category_exclusivity == 1.0

    # 2 ai + 2 aec → (2/4)^2 + (2/4)^2 = 0.5
    trades_2 = [
        (_trade("t1", "m1"), m_ai),
        (_trade("t2", "m2"), m_ai2),
        (_trade("t3", "m3"), m_aec),
        (_trade("t4", "m4"), m_aec2),
    ]
    p2 = wallet_profiler.compute("0xaf4", trades_2)
    assert abs(p2.category_exclusivity - 0.5) < 1e-9


def test_idempotent() -> None:
    """Re-running compute on identical inputs yields identical output
    (excluding as_of, which moves with wall clock)."""
    m = _market("m1")
    t = _trade("t1", "m1")
    p1 = wallet_profiler.compute("0xaf4", [(t, m)])
    p2 = wallet_profiler.compute("0xaf4", [(t, m)])
    for field in (
        "total_markets", "resolved_markets", "wins", "losses",
        "win_rate", "total_pnl_cents", "categories",
        "category_exclusivity", "avg_entry_to_resolution_hours",
    ):
        assert getattr(p1, field) == getattr(p2, field), field


def test_staleness_guard() -> None:
    # Fresh profile
    fresh = WalletProfile(wallet_address="0xaf4", as_of=now_utc())
    assert not wallet_profiler.is_stale(fresh)
    # 2h old → stale
    old = WalletProfile(
        wallet_address="0xaf4",
        as_of=now_utc() - timedelta(hours=2),
    )
    assert wallet_profiler.is_stale(old)
    assert wallet_profiler.is_stale(None)
