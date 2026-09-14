from types import SimpleNamespace
from unittest.mock import patch

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


def test_pullback_is_blocked_when_reversal_is_not_confirmed():
    sig = _signal("PULLBACK", "LONG")

    class Analyzer:
        def analyze_mtf(self, *args, **kwargs):
            return sig

    metrics = {
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
    }
    log = _Log()
    with patch.object(guard, "_pullback_metrics", return_value=metrics):
        guard.install(Analyzer, log)
        result = Analyzer().analyze_mtf("NEARUSDT", [{}] * 40, [], [])
    assert result is None
    assert any("[PULLBACK_CONFIRMATION]" in str(row[0]) for row in log.rows)


def test_pullback_passes_after_broad_reversal_confirmation():
    sig = _signal("PULLBACK", "LONG")

    class Analyzer:
        def analyze_mtf(self, *args, **kwargs):
            return sig

    metrics = {
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
    }
    log = _Log()
    with patch.object(guard, "_pullback_metrics", return_value=metrics):
        guard.install(Analyzer, log)
        result = Analyzer().analyze_mtf("NEARUSDT", [{}] * 40, [], [])
    assert result is sig


def test_intrabar_fast_path_requires_strict_five_of_five():
    sig = _signal("PULLBACK", "SHORT")

    class Analyzer:
        def analyze_mtf(self, *args, **kwargs):
            return sig

    closed = {
        "ok": False,
        "reason": "insufficient_reversal_votes",
        "votes": {"rsi_recovered": True},
        "vote_count": 1,
        "structure": "ACCUMULATION",
        "bos": False,
        "bos_dir": "NONE",
        "rsi": 41.0,
        "ema20_side": True,
    }
    fast = {
        "ok": True,
        "reason": "intrabar_5of5_confirmed",
        "vote_count": 5,
        "structure": "DOWNTREND",
        "ema20_side": True,
    }
    log = _Log()
    with patch.object(guard, "_pullback_metrics", return_value=closed), patch.object(
        guard, "_intrabar_fast_metrics", return_value=fast
    ):
        guard.install(Analyzer, log)
        result = Analyzer().analyze_mtf("ATOMUSDT", [{}] * 40, [], [])
    assert result is sig
    assert any("intrabar_fast" in str(row[0]) for row in log.rows)


def test_intrabar_fast_path_never_overrides_opposite_structure_block():
    sig = _signal("PULLBACK", "SHORT")

    class Analyzer:
        def analyze_mtf(self, *args, **kwargs):
            return sig

    closed = {
        "ok": False,
        "reason": "opposite_structure_not_reversed",
        "votes": {"body_aligned": True, "price_progress": True, "rsi_recovered": True},
        "vote_count": 3,
        "structure": "UPTREND",
        "bos": False,
        "bos_dir": "NONE",
        "rsi": 50.0,
        "ema20_side": False,
    }
    log = _Log()
    with patch.object(guard, "_pullback_metrics", return_value=closed), patch.object(
        guard, "_intrabar_fast_metrics"
    ) as fast:
        guard.install(Analyzer, log)
        result = Analyzer().analyze_mtf("ATOMUSDT", [{}] * 40, [], [])
    assert result is None
    fast.assert_not_called()


def test_intrabar_helper_rejects_four_of_five_even_if_closed_rule_would_pass():
    base = {
        "ok": True,
        "reason": "confirmed",
        "vote_count": 4,
        "ema20_side": True,
        "opposite_structure": False,
        "opposite_bos": False,
    }
    with patch.object(guard, "_metrics_from_data", return_value=base):
        out = guard._intrabar_fast_metrics([{}] * 31, "LONG")
    assert out["ok"] is False
    assert out["reason"] == "intrabar_not_strict_enough"


def test_intrabar_helper_rejects_opposite_structure_even_at_five_votes():
    base = {
        "ok": True,
        "reason": "confirmed",
        "vote_count": 5,
        "ema20_side": True,
        "opposite_structure": True,
        "opposite_bos": False,
    }
    with patch.object(guard, "_metrics_from_data", return_value=base):
        out = guard._intrabar_fast_metrics([{}] * 31, "SHORT")
    assert out["ok"] is False


def test_insufficient_history_fails_closed():
    out = guard._pullback_metrics([{"o": 1, "h": 1, "l": 1, "c": 1}] * 10, "LONG")
    assert out["ok"] is False
    assert out["reason"] == "insufficient_15m_history"


def test_prepared_series_drops_exactly_one_disposable_bar():
    rows = [{"i": i} for i in range(31)]
    confirmed = guard._confirmed_15m(rows)
    assert len(confirmed) == 30
    assert confirmed[-1]["i"] == 29


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
