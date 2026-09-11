from types import SimpleNamespace

from bot import htf_transition_shadow as shadow


def _bars(n, *, last=100.0):
    rows = []
    for i in range(n):
        px = last + i * 0.001
        rows.append({"o": px, "h": px + 0.1, "l": px - 0.1,
                     "c": px + 0.02, "v": 1000.0, "ts": i})
    return rows


def _reset():
    with shadow._LOCK:
        shadow._SEEN.clear(); shadow._SEEN_ORDER.clear(); shadow._ACTIVE.clear()
        shadow._METRICS["unique"] = 0; shadow._METRICS["eligible"] = 0
        shadow._METRICS["resolved"] = 0; shadow._METRICS["outcomes"].clear()
        shadow._METRICS["net_sum"] = 0.0; shadow._METRICS["mfe_sum"] = 0.0
        shadow._METRICS["mae_sum"] = 0.0


def test_transition_shadow_records_strong_forming_4h_flip(monkeypatch):
    _reset()
    states = iter(["LONG", "LONG", "SHORT", "LONG"])
    monkeypatch.setattr(shadow, "_ema_state", lambda closes: next(states))
    monkeypatch.setattr(shadow, "_atr_pair", lambda h, l, c: (1.0, 1.0))
    monkeypatch.setattr(shadow, "score_tf", lambda *a, **k: {
        "ok": True, "total": 85, "vol_r": 1.8, "adx_v": 30,
    })
    monkeypatch.setattr(shadow, "detect_entry", lambda *a, **k: (True, "MOMENTUM"))
    monkeypatch.setattr(shadow, "_update_outcomes", lambda *a, **k: None)
    emitted = []
    monkeypatch.setattr(shadow, "_emit", lambda log, tag, payload: emitted.append((tag, payload)))

    log = SimpleNamespace(debug=lambda *a, **k: None, info=lambda *a, **k: None)
    shadow.observe("ETHUSDT", _bars(100), _bars(100), _bars(100), None, log)

    snap = shadow.snapshot()
    assert snap["eligible"] == 1
    assert snap["active"] == 1
    assert emitted[0][0] == "HTF_TRANSITION_SHADOW"
    assert emitted[0][1]["closed_4h_state"] == "SHORT"
    assert emitted[0][1]["forming_4h_state"] == "LONG"
    assert emitted[0][1]["execution_effect"] == "NONE"


def test_transition_shadow_never_observes_when_production_already_signaled(monkeypatch):
    _reset()
    monkeypatch.setattr(shadow, "_update_outcomes", lambda *a, **k: None)
    log = SimpleNamespace(debug=lambda *a, **k: None, info=lambda *a, **k: None)
    shadow.observe(
        "ETHUSDT", _bars(100), _bars(100), _bars(100),
        SimpleNamespace(direction="LONG"), log,
    )
    assert shadow.snapshot()["eligible"] == 0
    assert shadow.snapshot()["active"] == 0


def test_transition_shadow_requires_forming_4h_flip(monkeypatch):
    _reset()
    states = iter(["LONG", "LONG", "SHORT", "SHORT"])
    monkeypatch.setattr(shadow, "_ema_state", lambda closes: next(states))
    monkeypatch.setattr(shadow, "_update_outcomes", lambda *a, **k: None)
    log = SimpleNamespace(debug=lambda *a, **k: None, info=lambda *a, **k: None)
    shadow.observe("ETHUSDT", _bars(100), _bars(100), _bars(100), None, log)
    assert shadow.snapshot()["eligible"] == 0
