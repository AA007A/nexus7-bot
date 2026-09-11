from bot import missed_opportunity_audit as audit


def test_directional_return_long_and_short():
    assert audit._directional_return_pct("LONG", 100.0, 110.0) == 10.0
    assert audit._directional_return_pct("SHORT", 100.0, 90.0) == 10.0
    assert audit._directional_return_pct("LONG", 100.0, 90.0) == -10.0
    assert audit._directional_return_pct("SHORT", 100.0, 110.0) == -10.0


def test_directional_return_invalid_prices_fail_to_zero():
    assert audit._directional_return_pct("LONG", 0.0, 100.0) == 0.0
    assert audit._directional_return_pct("SHORT", 100.0, 0.0) == 0.0


def test_signal_key_deduplicates_repeated_scans_inside_same_15m_bucket():
    first = audit._signal_key("NEARUSDT", "LONG", "BOS_BREAK", 1800.0)
    second = audit._signal_key("NEARUSDT", "LONG", "BOS_BREAK", 2699.9)
    assert first == second


def test_signal_key_separates_direction_setup_and_next_bucket():
    base = audit._signal_key("ETHUSDT", "LONG", "MOMENTUM", 900.0)
    assert base != audit._signal_key("ETHUSDT", "SHORT", "MOMENTUM", 900.0)
    assert base != audit._signal_key("ETHUSDT", "LONG", "BOS_BREAK", 900.0)
    assert base != audit._signal_key("ETHUSDT", "LONG", "MOMENTUM", 1800.0)


def test_round_trip_cost_is_explicit_and_positive():
    assert audit._ESTIMATED_ROUND_TRIP_COST_PCT > 0
    assert audit._ESTIMATED_ROUND_TRIP_COST_PCT < 1
