from types import SimpleNamespace

from bot import nexus_decision_consistency as consistency


def _candles(n):
    return [
        {"o": 1.0, "h": 1.1, "l": 0.9, "c": 1.0, "v": 10.0, "ts": i}
        for i in range(n)
    ]


def test_closed_mtf_removes_forming_candle_when_minimum_is_preserved():
    k15, k1h, k4h = consistency.closed_mtf(
        _candles(100), _candles(100), _candles(100)
    )
    assert len(k15) == 99
    assert len(k1h) == 99
    assert len(k4h) == 99
    assert k15[-1]["ts"] == 98
    assert k1h[-1]["ts"] == 98
    assert k4h[-1]["ts"] == 98


def test_closed_mtf_does_not_drop_below_nexus_minimum_history():
    k15, k1h, k4h = consistency.closed_mtf(
        _candles(60), _candles(40), _candles(20)
    )
    assert len(k15) == 60
    assert len(k1h) == 40
    assert len(k4h) == 20


def test_install_routes_decision_through_confirmed_candles_only():
    observed = {}

    def original(symbol, k15, k1h, k4h, *args, **kwargs):
        observed["symbol"] = symbol
        observed["lengths"] = (len(k15), len(k1h), len(k4h))
        observed["last_ts"] = (k15[-1]["ts"], k1h[-1]["ts"], k4h[-1]["ts"])
        return SimpleNamespace(decision="WAIT", setup_quality=0.0)

    fake_ai = SimpleNamespace(decide=original)
    fake_log = SimpleNamespace(info=lambda *args, **kwargs: None,
                               warning=lambda *args, **kwargs: None)

    consistency.install(fake_ai, fake_log)
    result = fake_ai.decide("ETHUSDT", _candles(100), _candles(100), _candles(100))

    assert result.decision == "WAIT"
    assert observed["symbol"] == "ETHUSDT"
    assert observed["lengths"] == (99, 99, 99)
    assert observed["last_ts"] == (98, 98, 98)


def test_install_is_idempotent():
    calls = {"n": 0}

    def original(symbol, k15, k1h, k4h, *args, **kwargs):
        calls["n"] += 1
        return SimpleNamespace(decision="WAIT", setup_quality=0.0)

    fake_ai = SimpleNamespace(decide=original)
    fake_log = SimpleNamespace(info=lambda *args, **kwargs: None,
                               warning=lambda *args, **kwargs: None)

    consistency.install(fake_ai, fake_log)
    first = fake_ai.decide
    consistency.install(fake_ai, fake_log)
    second = fake_ai.decide

    assert first is second
    second("BTCUSDT", _candles(100), _candles(100), _candles(100))
    assert calls["n"] == 1
