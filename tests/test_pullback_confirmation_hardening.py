from types import SimpleNamespace

from bot import pullback_confirmation_hardening as guard


class _Log:
    def __init__(self):
        self.rows = []

    def info(self, *args):
        self.rows.append(args)

    def warning(self, *args):
        self.rows.append(args)


def _signal(entry_type="PULLBACK", direction="LONG"):
    return SimpleNamespace(entry_type=entry_type, direction=direction)


def test_non_pullback_signal_is_unchanged_without_confirmation():
    sig = _signal("MOMENTUM")

    class Analyzer:
        def analyze_mtf(self, *args, **kwargs):
            return sig

    log = _Log()
    guard.install(Analyzer, log)
    result = Analyzer().analyze_mtf("BTCUSDT", [], [], [])
    assert result is sig


def test_pullback_is_blocked_when_reversal_is_not_confirmed(monkeypatch):
    sig = _signal("PULLBACK", "LONG")

    class Analyzer:
        def analyze_mtf(self, *args, **kwargs):
            return sig

    monkeypatch.setattr(guard, "_pullback_metrics", lambda *a, **k: {
        "ok": False,
        "reason": "opposite_structure_not_reversed",
        "votes": {"body_aligned": True, "price_progress": True},
        "vote_count": 2,
        "structure": "DOWNTREND",
        "bos": False,
        "bos_dir": "NONE",
        "rsi": 49.1,
        "macd_hist": -0.007,
        "macd_prev": -0.006,
        "ema20_side": False,
    })
    log = _Log()
    guard.install(Analyzer, log)
    result = Analyzer().analyze_mtf("NEARUSDT", [{}] * 40, [], [])
    assert result is None
    assert any("[PULLBACK_CONFIRMATION]" in str(row[0]) for row in log.rows)


def test_pullback_passes_after_broad_reversal_confirmation(monkeypatch):
    sig = _signal("PULLBACK", "LONG")

    class Analyzer:
        def analyze_mtf(self, *args, **kwargs):
            return sig

    monkeypatch.setattr(guard, "_pullback_metrics", lambda *a, **k: {
        "ok": True,
        "reason": "confirmed",
        "votes": {
            "body_aligned": True,
            "price_progress": True,
            "ema9_reclaim": True,
            "macd_turn": True,
            "rsi_recovered": True,
        },
        "vote_count": 5,
        "structure": "ACCUMULATION",
        "bos": True,
        "bos_dir": "BULLISH",
        "rsi": 52.0,
    })
    log = _Log()
    guard.install(Analyzer, log)
    result = Analyzer().analyze_mtf("NEARUSDT", [{}] * 40, [], [])
    assert result is sig


def test_insufficient_history_fails_closed():
    out = guard._pullback_metrics([{"o": 1, "h": 1, "l": 1, "c": 1}] * 10, "LONG")
    assert out["ok"] is False
    assert out["reason"] == "insufficient_15m_history"


def test_install_is_idempotent():
    calls = {"n": 0}
    sig = _signal("MOMENTUM")

    class Analyzer:
        def analyze_mtf(self, *args, **kwargs):
            calls["n"] += 1
            return sig

    log = _Log()
    guard.install(Analyzer, log)
    first = Analyzer.analyze_mtf
    guard.install(Analyzer, log)
    assert Analyzer.analyze_mtf is first
    Analyzer().analyze_mtf("BTCUSDT", [], [], [])
    assert calls["n"] == 1
