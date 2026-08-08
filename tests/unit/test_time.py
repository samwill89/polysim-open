"""utils/time.py tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from polysim.utils.time import iso, now_utc, parse_iso, parse_since


def test_now_utc_has_tz() -> None:
    assert now_utc().tzinfo == UTC


def test_iso_roundtrip() -> None:
    dt = datetime(2026, 4, 19, 14, 32, 11, tzinfo=UTC)
    assert parse_iso(iso(dt)) == dt


def test_parse_iso_assumes_utc_if_naive() -> None:
    assert parse_iso("2026-04-19T14:32:11").tzinfo == UTC


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        ("30s", timedelta(seconds=30)),
        ("15m", timedelta(minutes=15)),
        ("24h", timedelta(hours=24)),
        ("7d", timedelta(days=7)),
    ],
)
def test_parse_since(expr: str, expected: timedelta) -> None:
    assert parse_since(expr) == expected


@pytest.mark.parametrize("bad", ["", "24", "24x", "x24", "h24"])
def test_parse_since_rejects_garbage(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_since(bad)
