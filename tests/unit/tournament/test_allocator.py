"""Tournament allocator unit tests.

Pure-function tests over RunScore → AllocatorDecision. No DB, no IO.
"""

from __future__ import annotations

import pytest

from polysim.tournament.allocator import RunScore, TournamentAllocator


def _score(
    run_id: int,
    variant: str,
    *,
    n_positions: int = 50,
    n_resolved: int | None = None,
    realized: int = 0,
    unrealized: int = 0,
    starting: int = 1_000_000,
    paused: bool = False,
    current: int | None = None,
) -> RunScore:
    return RunScore(
        run_id=run_id,
        variant_name=variant,
        starting_balance_cents=starting,
        current_balance_cents=current if current is not None else starting,
        realized_pnl_cents=realized,
        unrealized_pnl_cents=unrealized,
        n_positions=n_positions,
        n_resolved_positions=(
            n_positions if n_resolved is None else n_resolved
        ),
        max_drawdown_pct=0.0,
        paused=paused,
    )


def test_empty_returns_no_decisions() -> None:
    a = TournamentAllocator()
    assert a.rebalance([]) == []


def test_single_run_holds() -> None:
    """One run on its own can't be retired (no peer to compare against)."""
    a = TournamentAllocator(min_trial_positions=10)
    out = a.rebalance([_score(1, "v1", n_positions=50, realized=0)])
    assert len(out) == 1
    assert out[0].verdict == "hold"


def test_clear_loser_gets_retired() -> None:
    a = TournamentAllocator(
        min_trial_positions=10,
        retire_threshold_pct=-0.15,
        retire_fraction=0.20,
    )
    scores = [
        _score(1, "winner", n_positions=50, realized=+100_000),   # +10%
        _score(2, "neutral_a", n_positions=50, realized=0),
        _score(3, "neutral_b", n_positions=50, realized=0),
        _score(4, "neutral_c", n_positions=50, realized=0),
        _score(5, "loser", n_positions=50, realized=-200_000),    # -20%
    ]
    decisions = {d.run_id: d for d in a.rebalance(scores)}
    assert decisions[5].verdict == "retire"
    assert "-20.0%" in decisions[5].reason
    # Winner shouldn't be retired.
    assert decisions[1].verdict != "retire"


def test_loser_under_threshold_stays_held() -> None:
    """A loser whose loss is shallower than the threshold is held, not retired."""
    a = TournamentAllocator(
        min_trial_positions=10, retire_threshold_pct=-0.15,
    )
    scores = [
        _score(1, "winner", n_positions=50, realized=+100_000),
        _score(2, "shallow_loser", n_positions=50, realized=-50_000),  # -5%
        _score(3, "neutral_a", n_positions=50, realized=0),
        _score(4, "neutral_b", n_positions=50, realized=0),
        _score(5, "neutral_c", n_positions=50, realized=0),
    ]
    decisions = {d.run_id: d for d in a.rebalance(scores)}
    assert decisions[2].verdict == "hold"


def test_loser_with_insufficient_trial_stays_held() -> None:
    """A run with only a handful of positions isn't statistically significant."""
    a = TournamentAllocator(min_trial_positions=30)
    scores = [
        _score(1, "winner", n_positions=50, realized=+100_000),
        _score(2, "neutral_a", n_positions=50, realized=0),
        _score(3, "neutral_b", n_positions=50, realized=0),
        _score(4, "neutral_c", n_positions=50, realized=0),
        _score(5, "early_loser", n_positions=5, realized=-200_000),  # tiny sample
    ]
    decisions = {d.run_id: d for d in a.rebalance(scores)}
    assert decisions[5].verdict == "hold"
    assert "insufficient resolved trial" in decisions[5].reason


def test_open_positions_do_not_satisfy_resolved_trial_gate() -> None:
    a = TournamentAllocator(min_trial_positions=10)
    scores = [
        _score(1, "winner", realized=100_000),
        _score(2, "neutral_a"),
        _score(3, "neutral_b"),
        _score(4, "neutral_c"),
        _score(5, "open_loser", realized=-200_000, n_resolved=0),
    ]
    decision = {d.run_id: d for d in a.rebalance(scores)}[5]
    assert decision.verdict == "hold"
    assert "0/10 outcomes" in decision.reason


def test_promote_revives_paused_winner() -> None:
    a = TournamentAllocator()
    scores = [
        _score(1, "v_winner", n_positions=50, realized=+100_000, paused=True),
        _score(2, "v2", n_positions=50, realized=0),
        _score(3, "v3", n_positions=50, realized=0),
        _score(4, "v4", n_positions=50, realized=0),
        _score(5, "v5", n_positions=50, realized=-50_000),
    ]
    decisions = {d.run_id: d for d in a.rebalance(scores)}
    assert decisions[1].verdict == "promote"


def test_paused_neutral_run_stays_paused() -> None:
    """Pausing isn't auto-reversed unless the run is a top-tier winner."""
    a = TournamentAllocator()
    scores = [
        _score(1, "v1", n_positions=50, realized=0, paused=True),
        _score(2, "v2", n_positions=50, realized=+50_000),
        _score(3, "v3", n_positions=50, realized=+20_000),
        _score(4, "v4", n_positions=50, realized=0),
        _score(5, "v5", n_positions=50, realized=-30_000),
    ]
    decisions = {d.run_id: d for d in a.rebalance(scores)}
    assert decisions[1].verdict == "hold"


def test_ranking_is_by_total_return_not_just_realized() -> None:
    """Unrealized counts equally toward the ranking."""
    a = TournamentAllocator(min_trial_positions=10)
    scores = [
        _score(1, "real_winner", n_positions=50, realized=+50_000),
        _score(2, "unreal_winner", n_positions=50, realized=0, unrealized=+200_000),
        _score(3, "neutral", n_positions=50, realized=0),
        _score(4, "neutral", n_positions=50, realized=0),
        _score(5, "loser", n_positions=50, realized=-200_000),
    ]
    decisions = sorted(a.rebalance(scores), key=lambda d: d.rank)
    # unreal_winner (+20%) should outrank real_winner (+5%).
    assert decisions[0].run_id == 2
    assert decisions[1].run_id == 1


def test_retire_fraction_out_of_range_raises() -> None:
    with pytest.raises(ValueError):
        TournamentAllocator(retire_fraction=0.0)
    with pytest.raises(ValueError):
        TournamentAllocator(retire_fraction=1.0)
