import time

from bot import market_risk_binance_fallback as fallback


def test_premium_index_converts_decimal_funding_to_percent():
    now_ms = int(time.time() * 1000)
    values, observed = fallback.parse_premium_index({
        "lastFundingRate": "0.0008",
        "time": now_ms,
    })
    assert round(values["funding_rate_pct"], 6) == 0.08
    assert abs(observed["funding_rate_pct"] - (now_ms / 1000.0)) < 0.001


def test_premium_index_missing_funding_is_fail_neutral():
    values, observed = fallback.parse_premium_index({"time": int(time.time() * 1000)})
    assert values == {}
    assert observed == {}


def test_open_interest_history_computes_one_hour_percent_change():
    now_ms = int(time.time() * 1000)
    values, observed = fallback.parse_open_interest_history([
        {
            "symbol": "BTCUSDT",
            "sumOpenInterestValue": "10000000000",
            "timestamp": now_ms - 3_600_000,
        },
        {
            "symbol": "BTCUSDT",
            "sumOpenInterestValue": "11200000000",
            "timestamp": now_ms,
        },
    ])
    assert round(values["open_interest_change_pct"], 2) == 12.0
    assert abs(observed["open_interest_change_pct"] - (now_ms / 1000.0)) < 0.001


def test_open_interest_history_sorts_rows_by_source_timestamp():
    now_ms = int(time.time() * 1000)
    values, _ = fallback.parse_open_interest_history([
        {
            "sumOpenInterestValue": "10500000000",
            "timestamp": now_ms,
        },
        {
            "sumOpenInterestValue": "10000000000",
            "timestamp": now_ms - 3_600_000,
        },
    ])
    assert round(values["open_interest_change_pct"], 2) == 5.0


def test_open_interest_invalid_baseline_is_fail_neutral():
    now_ms = int(time.time() * 1000)
    values, observed = fallback.parse_open_interest_history([
        {"sumOpenInterestValue": "0", "timestamp": now_ms - 3_600_000},
        {"sumOpenInterestValue": "100", "timestamp": now_ms},
    ])
    assert values == {}
    assert observed == {}


def test_fallback_only_needed_when_primary_field_not_fresh():
    class Runtime:
        @staticmethod
        def snapshot():
            return {"signals": {"funding_rate_pct": 0.01}}

    assert fallback._field_needs_fallback(Runtime, "funding_rate_pct") is False
    assert fallback._field_needs_fallback(Runtime, "open_interest_change_pct") is True


def test_install_adds_source_age_guards_without_changing_thresholds():
    class Runtime:
        _SOURCE_MAX_AGE_S = {}

    class Scoring:
        _market_risk_binance_fallback_installed = False

        @staticmethod
        async def news_reader_loop():
            return None

    class Log:
        @staticmethod
        def warning(*args, **kwargs):
            return None

    fallback.install(Scoring, Runtime, Log())
    assert Runtime._SOURCE_MAX_AGE_S["funding_rate_pct"] == 2 * 3600.0
    assert Runtime._SOURCE_MAX_AGE_S["open_interest_change_pct"] == 2 * 3600.0
    assert Scoring._market_risk_binance_fallback_installed is True
