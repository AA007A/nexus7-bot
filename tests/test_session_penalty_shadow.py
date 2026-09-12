import inspect
from collections import Counter

from bot.engine import TradingEngine
from bot import session_penalty_shadow as shadow


class _Log:
    def __init__(self):
        self.lines = []

    def info(self, message, *args):
        self.lines.append(message % args if args else message)

    def debug(self, message, *args):
        self.lines.append(message % args if args else message)


class _Signal:
    symbol = "DOGEUSDT"
    direction = "SHORT"
    score = 64
    entry = 100.0
    sl = 101.0
    tp = 98.0
    entry_type = "PULLBACK"
    regime = "TRENDING_DOWN"


def _bars(last_ts=10):
    return [
        {"ts": i, "o": 100.0, "h": 100.2, "l": 99.8, "c": 100.0, "v": 1000.0}
        for i in range(1, last_ts + 1)
    ]


def _reset():
    with shadow._LOCK:
        shadow._SEEN.clear()
        shadow._ACTIVE.clear()
        shadow._METRICS["unique"] = 0
        shadow._METRICS["eligible"] = 0
        shadow._METRICS["resolved"] = 0
        shadow._METRICS["outcomes"] = Counter()
        shadow._METRICS["sessions"] = Counter()
        shadow._METRICS["symbols"] = Counter()


def test_doge_asia_penalty_is_observed_without_changing_production_signal():
    _reset()
    original_session = TradingEngine.__dict__["_get_market_session"]
    original_closed = shadow.closed_mtf
    TradingEngine._get_market_session = staticmethod(lambda: "ASIA")
    shadow.closed_mtf = lambda k15, k1h, k4h: (list(k15), list(k1h), list(k4h))
    signal = _Signal()
    try:
        shadow.observe(
            "DOGEUSDT", _bars(), _bars(), _bars(),
            production_result=signal, min_score=60, log=_Log(),
        )
    finally:
        TradingEngine._get_market_session = original_session
        shadow.closed_mtf = original_closed

    snap = shadow.snapshot()
    assert signal.score == 64
    assert snap["eligible"] == 1
    assert snap["active"] == 1
    state = next(iter(shadow._ACTIVE.values()))
    assert state["session"] == "ASIA"
    assert state["penalty"] == -10
    assert state["base_score"] == 64
    assert state["adjusted_score"] == 54
    assert state["min_score"] == 60


def test_signal_not_killed_by_session_penalty_is_not_enrolled():
    _reset()
    original_session = TradingEngine.__dict__["_get_market_session"]
    original_closed = shadow.closed_mtf
    TradingEngine._get_market_session = staticmethod(lambda: "LONDON")
    shadow.closed_mtf = lambda k15, k1h, k4h: (list(k15), list(k1h), list(k4h))
    try:
        shadow.observe(
            "DOGEUSDT", _bars(), _bars(), _bars(),
            production_result=_Signal(), min_score=60, log=_Log(),
        )
    finally:
        TradingEngine._get_market_session = original_session
        shadow.closed_mtf = original_closed

    assert shadow.snapshot()["eligible"] == 0
    assert shadow.snapshot()["active"] == 0


def test_same_bar_tp_and_sl_resolves_stop_first():
    _reset()
    key = "DOGEUSDT:SHORT:1"
    with shadow._LOCK:
        shadow._ACTIVE[key] = {
            "symbol": "DOGEUSDT", "direction": "SHORT", "session": "ASIA",
            "penalty": -10, "base_score": 64, "adjusted_score": 54,
            "entry": 100.0, "sl": 101.0, "tp": 98.0,
            "last_bar_ts": 1, "bars": 0, "mfe_pct": 0.0, "mae_pct": 0.0,
        }
    original_closed = shadow.closed_mtf
    shadow.closed_mtf = lambda k15, k1h, k4h: (
        [{"ts": 2, "o": 100.0, "h": 102.0, "l": 97.0, "c": 99.0, "v": 1.0}], [], []
    )
    try:
        shadow._update_outcomes("DOGEUSDT", [{}], [], [], _Log())
    finally:
        shadow.closed_mtf = original_closed

    snap = shadow.snapshot()
    assert snap["active"] == 0
    assert snap["resolved"] == 1
    assert snap["outcomes"]["AMBIGUOUS_STOP_FIRST"] == 1


def test_session_shadow_reads_production_policy_instead_of_copying_table():
    source = inspect.getsource(shadow)
    assert "TradingEngine._SESSION_PENALTY" in source
    assert '"DOGEUSDT": -10' not in source
    assert '"AVAXUSDT": -8' not in source


def test_session_shadow_has_no_execution_or_threshold_mutation():
    source = inspect.getsource(shadow)
    forbidden = (
        "place_order", "create_order", "cancel_order", "close_position",
        "_nexus_validate", "nexus_ai.decide", "cfg.LEVERAGE =",
        "MIN_ENTRY_SCORE =", "NEXUS_MIN_SCORE =", "MIN_VOLUME_MULT =",
        "_SESSION_PENALTY =",
    )
    assert all(token not in source for token in forbidden)
