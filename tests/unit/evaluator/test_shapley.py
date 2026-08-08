"""Shapley attribution tests — build plan §5.3."""

from __future__ import annotations

import json

from polysim.evaluator.shapley import attribute_closed_positions


def _flag(
    flag_id: int, *, contributions: dict[str, float]
) -> dict[str, object]:
    return {
        "id": flag_id,
        "components_json": json.dumps(
            {"contribution_by_detector": contributions}
        ),
    }


def _pos(
    pid: int, *, source_flag_id: int | None, realized: int
) -> dict[str, object]:
    return {
        "id": pid,
        "source_flag_id": source_flag_id,
        "realized_pnl_cents": realized,
    }


def test_single_position_split_proportionally() -> None:
    flags = {1: _flag(1, contributions={"A": 3.5, "B": 1.5, "C": 0.0})}
    # Total = 5.0; A share = 70%, B = 30%, C = 0.
    positions = [_pos(10, source_flag_id=1, realized=1000)]
    out = attribute_closed_positions(positions, flags)
    assert out["A"] == 700
    assert out["B"] == 300
    assert "C" not in out or out["C"] == 0


def test_multiple_positions_sum() -> None:
    flags = {
        1: _flag(1, contributions={"A": 1.0, "B": 1.0}),
        2: _flag(2, contributions={"A": 1.0}),
    }
    positions = [
        _pos(10, source_flag_id=1, realized=1000),  # A=500, B=500
        _pos(11, source_flag_id=2, realized=200),   # A=200
    ]
    out = attribute_closed_positions(positions, flags)
    assert out["A"] == 700
    assert out["B"] == 500


def test_zero_contribution_skipped() -> None:
    flags = {1: _flag(1, contributions={"A": 0.0, "B": 0.0})}
    positions = [_pos(10, source_flag_id=1, realized=1000)]
    out = attribute_closed_positions(positions, flags)
    assert out == {}


def test_missing_flag_skipped() -> None:
    flags: dict[int, dict[str, object]] = {}
    positions = [_pos(10, source_flag_id=1, realized=1000)]
    out = attribute_closed_positions(positions, flags)
    assert out == {}


def test_losing_position_attributes_negative() -> None:
    flags = {1: _flag(1, contributions={"A": 2.0, "B": 3.0})}
    positions = [_pos(10, source_flag_id=1, realized=-500)]
    out = attribute_closed_positions(positions, flags)
    # Losses split in same proportion (2/5 and 3/5)
    assert out["A"] == -200
    assert out["B"] == -300


def test_components_as_dict_not_json() -> None:
    """Some codepaths may hand us a pre-parsed dict."""
    flag = {
        "id": 1,
        "components_json": {"contribution_by_detector": {"A": 1.0}},
    }
    positions = [_pos(10, source_flag_id=1, realized=1000)]
    out = attribute_closed_positions(positions, {1: flag})
    assert out["A"] == 1000
