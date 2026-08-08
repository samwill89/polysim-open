from polysim.trading.costs import market_fees_enabled, taker_fee_cents


def test_politics_dynamic_taker_fee_matches_category_fallback() -> None:
    assert (
        taker_fee_cents(
            shares=100,
            price_cents=50,
            category="politics",
            fees_enabled=True,
        )
        == 100
    )


def test_fee_is_symmetric_and_disabled_markets_are_free() -> None:
    low = taker_fee_cents(shares=100, price_cents=30, category="politics", fees_enabled=True)
    high = taker_fee_cents(shares=100, price_cents=70, category="politics", fees_enabled=True)
    assert low == high == 84
    assert taker_fee_cents(shares=100, price_cents=50, category="politics", fees_enabled=False) == 0


def test_fee_enabled_metadata_parsing() -> None:
    assert market_fees_enabled({"feesEnabled": True}) is True
    assert market_fees_enabled({"feesEnabled": "true"}) is True
    assert market_fees_enabled({}) is False


def test_market_fee_schedule_overrides_category_and_uses_exponent() -> None:
    metadata = {
        "feesEnabled": True,
        "feeSchedule": {"rate": 0.02, "exponent": 2, "takerOnly": True},
    }
    assert (
        taker_fee_cents(
            shares=100,
            price_cents=50,
            category="politics",
            metadata=metadata,
        )
        == 13
    )


def test_current_sports_fallback_rate() -> None:
    assert (
        taker_fee_cents(
            shares=100,
            price_cents=50,
            category="sports",
            fees_enabled=True,
        )
        == 125
    )


def test_subcent_venue_fee_rounds_up_in_cent_ledger() -> None:
    assert (
        taker_fee_cents(
            shares=1,
            price_cents=1,
            category="politics",
            fees_enabled=True,
        )
        == 1
    )
