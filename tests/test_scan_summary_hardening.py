from bot.scan_summary_hardening import latest_unique


def test_latest_unique_keeps_newest_record_per_symbol():
    rows = [
        {"symbol": "BTCUSDT", "score": 40, "ts": 1},
        {"symbol": "NEARUSDT", "score": 70, "ts": 2},
        {"symbol": "LTCUSDT", "score": 55, "ts": 3},
        {"symbol": "NEARUSDT", "score": 74, "ts": 4},
        {"symbol": "LTCUSDT", "score": 57, "ts": 5},
    ]

    out = latest_unique(rows, 12)

    assert [x["symbol"] for x in out] == ["LTCUSDT", "NEARUSDT", "BTCUSDT"]
    assert [x["score"] for x in out] == [57, 74, 40]


def test_latest_unique_respects_limit_and_ignores_bad_rows():
    rows = [
        None,
        {"score": 99},
        {"symbol": "BTCUSDT", "score": 50},
        {"symbol": "ETHUSDT", "score": 60},
    ]

    out = latest_unique(rows, 1)

    assert out == [{"symbol": "ETHUSDT", "score": 60}]


def test_latest_unique_zero_limit_is_empty():
    assert latest_unique([{"symbol": "BTCUSDT", "score": 50}], 0) == []
