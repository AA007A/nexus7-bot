from bot import liquidation as liq


def test_max_leverage_matches_analyze_boundary():
    stop_pct = 2.0
    max_lev = liq.max_leverage_for_stop(stop_pct, "NEARUSDT")

    # For the default 0.4% MMR + 0.06% liquidation fee and 0.30% safety gap,
    # 2.0% stop requires about 36x or less. The old inversion incorrectly
    # reported ~54x even though analyze() rejected 50x.
    assert 35 <= max_lev <= 36

    entry = 100.0
    stop = entry * (1.0 - stop_pct / 100.0)
    safe = liq.analyze(entry, stop, max_lev, True, symbol="NEARUSDT")
    unsafe = liq.analyze(entry, stop, max_lev + 1, True, symbol="NEARUSDT")

    assert safe.stop_effective is True
    assert unsafe.stop_effective is False


def test_fifty_x_is_not_reported_safe_for_two_percent_stop():
    assert liq.max_leverage_for_stop(2.0, "NEARUSDT") < 50
