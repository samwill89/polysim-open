"""Pure parsing helpers for Polymarket payloads.

Shared between `polymarket_rest.py` and `polymarket_ws.py`.  Every parser
here returns `None` (never raises) on malformed input — upstream callers
decide whether to log, count, or skip.

The parsers are intentionally forgiving about field naming because the
Polymarket APIs (Gamma, Data, CLOB-WS) use different cases and keys for
the same concepts. Fields we care about are always looked up under
several plausible names.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Literal, cast

from polysim.models import Market, TradeEvent


def parse_timestamp(v: Any) -> datetime | None:
    """Accept Unix seconds/ms, ISO 8601, or datetime; return a UTC datetime."""
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=UTC)
    if isinstance(v, (int, float)):
        x = float(v)
        # Heuristic: > 10^12 → milliseconds
        if x > 10**12:
            x /= 1000.0
        try:
            return datetime.fromtimestamp(x, tz=UTC)
        except (ValueError, OverflowError, OSError):
            return None
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        # datetime.fromisoformat handles trailing Z in 3.11+ but strip to be safe
        s = s.rstrip("Z")
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
        return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
    return None


def parse_price_cents(v: Any) -> int | None:
    """Return integer cents in [0, 100]. Accepts either:
      - 0..1 decimal (Polymarket Data/CLOB convention), or
      - integer 0..100 (percent form used in some Gamma fields).
    1.5 (neither 0..1 nor integer percent) is rejected.
    """
    if isinstance(v, bool):
        return None
    try:
        price = float(v)
    except (TypeError, ValueError):
        return None
    if 0.0 <= price <= 1.0:
        return round(price * 100)
    # Integer percent form: 32, 50, 100.0
    if 1.0 < price <= 100.0 and abs(price - round(price)) < 1e-9:
        return round(price)
    return None


def parse_size_shares(v: Any) -> int | None:
    """Coerce to non-negative integer share count."""
    try:
        size = float(v)
    except (TypeError, ValueError):
        return None
    if size < 0:
        return None
    return round(size)


def parse_side(v: Any) -> Literal["BUY", "SELL"] | None:
    # Check bool BEFORE int — bool is a subclass of int in Python.
    if isinstance(v, bool):
        return None
    if isinstance(v, str):
        u = v.upper()
        if u in ("BUY", "SELL"):
            return cast(Literal["BUY", "SELL"], u)
    if isinstance(v, int) and v in (0, 1):
        return "BUY" if v == 0 else "SELL"
    return None


def parse_outcome(v: Any) -> Literal["YES", "NO"] | None:
    # Check bool BEFORE int — bool is a subclass of int in Python.
    if isinstance(v, bool):
        return "YES" if v else "NO"
    if isinstance(v, str):
        u = v.upper()
        if u in ("YES", "NO"):
            return cast(Literal["YES", "NO"], u)
        if u in ("TRUE", "Y"):
            return "YES"
        if u in ("FALSE", "N"):
            return "NO"
    if isinstance(v, int) and v in (0, 1):
        return "YES" if v == 0 else "NO"
    return None


def parse_resolution_outcome(v: Any) -> Literal["YES", "NO", "INVALID"] | None:
    basic = parse_outcome(v)
    if basic is not None:
        return basic
    if isinstance(v, str) and v.upper() == "INVALID":
        return "INVALID"
    return None


def parse_resolution_from_outcome_prices(
    outcomes_raw: Any, prices_raw: Any
) -> Literal["YES", "NO", "INVALID"] | None:
    """Gamma encodes resolution as parallel JSON arrays in `outcomes` and
    `outcomePrices`. The outcome whose price ~= 1 is the winner. Both
    arrays are JSON-encoded strings in the API response.

    Only handles binary YES/NO markets — multi-outcome (3+ buckets) is
    out of scope for our schema. If both prices are ~0, market is invalid.
    """
    import contextlib
    import json as _json
    outcomes_list: list[str] | None = None
    prices_list: list[float] | None = None
    if isinstance(outcomes_raw, str):
        with contextlib.suppress(ValueError, TypeError):
            parsed = _json.loads(outcomes_raw)
            if isinstance(parsed, list):
                outcomes_list = [str(x) for x in parsed]
    elif isinstance(outcomes_raw, list):
        outcomes_list = [str(x) for x in outcomes_raw]
    if isinstance(prices_raw, str):
        with contextlib.suppress(ValueError, TypeError):
            parsed = _json.loads(prices_raw)
            if isinstance(parsed, list):
                prices_list = [float(x) for x in parsed]
    elif isinstance(prices_raw, list):
        with contextlib.suppress(ValueError, TypeError):
            prices_list = [float(x) for x in prices_raw]
    if not outcomes_list or not prices_list:
        return None
    if len(outcomes_list) != len(prices_list):
        return None
    # Binary-only.
    if len(outcomes_list) != 2:
        return None
    names = [o.strip().upper() for o in outcomes_list]
    if set(names) != {"YES", "NO"}:
        return None
    yes_idx = names.index("YES")
    no_idx = names.index("NO")
    yes_p = prices_list[yes_idx]
    no_p = prices_list[no_idx]
    if yes_p > 0.5 and yes_p > no_p:
        return "YES"
    if no_p > 0.5 and no_p > yes_p:
        return "NO"
    if yes_p < 0.05 and no_p < 0.05:
        return "INVALID"
    return None


def parse_gamma_market(raw: Any) -> Market | None:
    """Convert a Gamma API market object to our `Market` model.

    Accepts shape variations across Gamma's endpoints. Returns None if
    the object is missing the required fields (id, slug, question,
    created_at).
    """
    if not isinstance(raw, Mapping):
        return None

    market_id = raw.get("conditionId") or raw.get("condition_id") or raw.get("id")
    slug = raw.get("slug")
    question = raw.get("question") or raw.get("title")
    created = parse_timestamp(raw.get("createdAt") or raw.get("created_at"))
    if not (market_id and slug and question and created):
        return None

    resolves_at = parse_timestamp(
        raw.get("endDate") or raw.get("end_date") or raw.get("end_date_iso")
    )
    resolved_at = parse_timestamp(raw.get("resolvedAt") or raw.get("resolved_at"))
    resolved_outcome = parse_resolution_outcome(
        raw.get("resolvedOutcome") or raw.get("outcome")
    )
    # Fall back to outcomes + outcomePrices if explicit fields aren't there
    # (Gamma's standard response for closed binary markets).
    if resolved_outcome is None and raw.get("closed"):
        resolved_outcome = parse_resolution_from_outcome_prices(
            raw.get("outcomes"), raw.get("outcomePrices"),
        )
        if resolved_outcome is not None and resolved_at is None:
            # Use end-date as a proxy for resolution timestamp.
            resolved_at = resolves_at

    volume_raw = raw.get("volume") or raw.get("volume24hr") or raw.get("volume_24hr")
    volume_cents: int | None = None
    if volume_raw is not None:
        try:
            volume_cents = round(float(volume_raw) * 100)
        except (TypeError, ValueError):
            volume_cents = None

    metadata = {k: v for k, v in raw.items() if k not in {"question", "slug", "id"}}

    return Market(
        id=str(market_id),
        slug=str(slug),
        question=str(question),
        category=None,  # filled in by Classifier
        created_at=created,
        resolves_at=resolves_at,
        resolved_outcome=resolved_outcome,
        resolved_at=resolved_at,
        daily_volume_usd_cents=volume_cents,
        metadata=metadata,
    )


def parse_trade(raw: Any) -> TradeEvent | None:
    """Convert a Data API or WS trade object to our `TradeEvent` model.

    Takes the TAKER as the wallet of interest; spec §6 says "one row per
    fill". Maker is preserved in metadata upstream if needed.
    """
    if not isinstance(raw, Mapping):
        return None

    tx = raw.get("transactionHash") or raw.get("tx_hash") or raw.get("hash")
    tid_raw = raw.get("id") or raw.get("tradeId")
    if tid_raw is None and tx:
        tid_raw = f"{tx}_{raw.get('eventIndex', 0)}"
    wallet = (
        raw.get("taker")
        or raw.get("user")
        or raw.get("owner")
        or raw.get("proxyWallet")
    )
    market = raw.get("market") or raw.get("marketId") or raw.get("conditionId")

    side = parse_side(raw.get("side"))
    outcome = parse_outcome(raw.get("outcome") or raw.get("outcomeSide"))
    size = parse_size_shares(raw.get("size") or raw.get("shares") or raw.get("amount"))
    price_cents = parse_price_cents(raw.get("price"))
    ts = parse_timestamp(
        raw.get("timestamp") or raw.get("time") or raw.get("matchTime")
    )

    if not (
        tid_raw
        and wallet
        and market
        and side
        and outcome
        and size is not None
        and price_cents is not None
        and ts
    ):
        return None

    return TradeEvent(
        id=str(tid_raw),
        wallet_address=str(wallet).lower(),
        market_id=str(market),
        side=side,
        outcome=outcome,
        size_shares=size,
        price_cents=price_cents,
        timestamp=ts,
        tx_hash=str(tx).lower() if isinstance(tx, str) else None,
    )
