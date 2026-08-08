"""Daily summary scheduler tests — build plan §6.4."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from polysim.reporter.scheduler import next_fire_time


class TestNextFireTime:
    def test_today_in_future(self) -> None:
        tz = ZoneInfo("UTC")
        now = datetime(2026, 4, 19, 6, 0, tzinfo=tz)
        nxt = next_fire_time(now=now, fire_hour_local=9, tz=tz)
        assert nxt == datetime(2026, 4, 19, 9, 0, tzinfo=tz)

    def test_today_in_past_rolls_over(self) -> None:
        tz = ZoneInfo("UTC")
        now = datetime(2026, 4, 19, 10, 0, tzinfo=tz)
        nxt = next_fire_time(now=now, fire_hour_local=9, tz=tz)
        assert nxt == datetime(2026, 4, 20, 9, 0, tzinfo=tz)

    def test_exact_hour_rolls_to_next_day(self) -> None:
        tz = ZoneInfo("UTC")
        now = datetime(2026, 4, 19, 9, 0, tzinfo=tz)
        nxt = next_fire_time(now=now, fire_hour_local=9, tz=tz)
        # Equal -> next day (strictly future per docstring)
        assert nxt == datetime(2026, 4, 20, 9, 0, tzinfo=tz)

    def test_with_tz_conversion(self) -> None:
        tz = ZoneInfo("America/New_York")
        # It's 14:00 UTC = 10:00 EDT in summer. Next 9am EDT is tomorrow.
        now = datetime(2026, 7, 15, 14, 0, tzinfo=UTC)
        nxt = next_fire_time(now=now, fire_hour_local=9, tz=tz)
        expected = datetime(2026, 7, 16, 9, 0, tzinfo=tz)
        assert nxt == expected
