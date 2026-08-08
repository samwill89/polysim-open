"""Time helpers — UTC everywhere, ISO 8601 strings for SQLite."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


def now_utc() -> datetime:
    return datetime.now(tz=UTC)


def iso(dt: datetime) -> str:
    """Format a datetime as UTC ISO 8601."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()


def parse_iso(s: str) -> datetime:
    """Parse an ISO 8601 string, always returning a UTC datetime."""
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def parse_since(expr: str) -> timedelta:
    """Parse '24h', '7d', '30m', '45s' into a timedelta."""
    expr = expr.strip().lower()
    if not expr:
        raise ValueError("empty time expression")
    unit = expr[-1]
    try:
        value = int(expr[:-1])
    except ValueError as e:
        raise ValueError(f"bad time expression: {expr!r}") from e
    if unit == "s":
        return timedelta(seconds=value)
    if unit == "m":
        return timedelta(minutes=value)
    if unit == "h":
        return timedelta(hours=value)
    if unit == "d":
        return timedelta(days=value)
    raise ValueError(f"unknown unit {unit!r} in {expr!r} (expected s/m/h/d)")
