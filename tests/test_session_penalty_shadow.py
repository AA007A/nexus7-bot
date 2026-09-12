import asyncio
import inspect
import sys
from collections import Counter
from types import SimpleNamespace

from bot.engine import TradingEngine
from bot.nexus_types import NexusDecision
from bot import session_penalty_shadow as shadow


class _Log:
    def __init__(self):
        self.lines = []

    def info(self, message, *args):
        self.lines.append(message % args if args else message)

    def warning(self, message, *args):
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


class _NexusEngine:
    def __init__(self, approved=True):
        self.calls = 0
        self.approved = approved

    async def _nexus_validate(self, sig):
        self.calls += 1
        if not self.approved:
            return NexusDecision.wait(sig.symbol, "ensemble veto", 100.0)
        return NexusDecision(
            symbol=sig.symbol,
            decision=sig.direction,
            confidence=72.0,
            setup_quality=68.0,
            market_regime="TRENDING_BEAR",
            entry=sig.entry,
            stop_loss=sig.sl,
            take_profit=sig.tp,
            risk_reward=2.0,
            expected_value=0.25,
            data_quality=100.0,
            execution_allowed=True,
            reasoning=["approved"],
        )


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
        shadow._METRICS["restored"] = 0
        shadow._METRICS["outcomes"] = Counter()
        shadow._METRICS["sessions"] = Counter()
        shadow._METRICS["symbols"] = Counter()
        shadow._METRICS["nexus_counterfactuals"] = 0
        shadow._METRICS["nexus_approved"] = 0
        shadow._METRICS["nexus_vetoed"] = 0
        shadow._METRICS["nexus_timeout"] = 0
        shadow._METRICS["nexus_error"] = 0
        shadow._METRICS["nexus_schedule_unavailable"] = 0
        shadow._METRICS["nexus_schedule_recovered"] = 0


def _enroll_asia_doge():
    original_session = TradingEngine.__dict__["_get_market_session"]
    original_closed = shadow.closed_mtf
    original_runtime_engine = shadow._runtime_engine
    TradingEngine._get_market_session = staticmethod(lambda: "ASIA")
    shadow.closed_mtf = lambda k15, k1h, k4h: (list(k15), list(k1h), list(k4h))
    shadow._runtime_engine = lambda: None
    signal = _Signal()
    try:
        shadow.observe(
            "DOGEUSDT", _bars(), _bars(), _bars(),
            production_result=signal, min_score=60, log=_Log(),
        )
    finally:
        TradingEngine._get_market_session = original_session
        shadow.closed_mtf = original_closed
        shadow._runtime_engine = original_runtime_engine
    return signal


def test_doge_asia_penalty_is_observed_without_changing_production_signal():
    _reset()
    signal = _enroll_asia_doge()
    snap = shadow.snapshot()

    assert signal.score == 64
    assert snap["eligible"] == 1
    assert snap["active"] == 1
    assert snap["nexus_schedule_unavailable"] == 1
    state = next(iter(shadow._ACTIVE.values()))
    assert state["session"] == "ASIA"
    assert state["penalty"] == -10
    assert state["base_score"] == 64
    assert state["adjusted_score"] == 54
    assert state["min_score"] == 60
    assert state["nexus_status"] == "SCHEDULE_UNAVAILABLE"
    assert state["nexus_reason"] == "runtime_engine_unavailable"
    assert state["nexus_schedule_pending"] is True


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
            "nexus_status": "VETOED", "nexus_approved": False,
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


def test_exact_nexus_counterfactual_uses_live_validator_once():
    _reset()
    signal = _enroll_asia_doge()
    engine = _NexusEngine(approved=True)
    original_closed = shadow.closed_mtf
    shadow.closed_mtf = lambda k15, k1h, k4h: (list(k15), list(k1h), list(k4h))
    try:
        asyncio.run(shadow.observe_nexus_counterfactual(
            engine, signal, _bars(), _bars(), _bars(), _Log(), timeout_s=1.0,
        ))
        asyncio.run(shadow.observe_nexus_counterfactual(
            engine, signal, _bars(), _bars(), _bars(), _Log(), timeout_s=1.0,
        ))
    finally:
        shadow.closed_mtf = original_closed

    snap = shadow.snapshot()
    state = next(iter(shadow._ACTIVE.values()))
    assert engine.calls == 1
    assert snap["nexus_counterfactuals"] == 1
    assert snap["nexus_approved"] == 1
    assert snap["nexus_vetoed"] == 0
    assert state["nexus_status"] == "APPROVED"
    assert state["nexus_approved"] is True
    assert state["nexus_setup_quality"] == 68.0


def test_exact_nexus_veto_is_recorded_fail_closed():
    _reset()
    signal = _enroll_asia_doge()
    engine = _NexusEngine(approved=False)
    original_closed = shadow.closed_mtf
    shadow.closed_mtf = lambda k15, k1h, k4h: (list(k15), list(k1h), list(k4h))
    try:
        asyncio.run(shadow.observe_nexus_counterfactual(
            engine, signal, _bars(), _bars(), _bars(), _Log(), timeout_s=1.0,
        ))
    finally:
        shadow.closed_mtf = original_closed

    snap = shadow.snapshot()
    state = next(iter(shadow._ACTIVE.values()))
    assert snap["nexus_approved"] == 0
    assert snap["nexus_vetoed"] == 1
    assert state["nexus_status"] == "VETOED"
    assert state["nexus_approved"] is False
    assert "ensemble veto" in state["nexus_reason"]


def test_no_running_loop_is_explicit_and_retries_when_loop_recovers():
    _reset()
    engine = _NexusEngine(approved=True)
    signal = _Signal()
    log = _Log()
    original_session = TradingEngine.__dict__["_get_market_session"]
    original_closed = shadow.closed_mtf
    original_runtime_engine = shadow._runtime_engine
    original_schedule_restore = shadow._schedule_restore
    original_schedule_persist = shadow._schedule_persist
    TradingEngine._get_market_session = staticmethod(lambda: "ASIA")
    shadow.closed_mtf = lambda k15, k1h, k4h: (list(k15), list(k1h), list(k4h))
    shadow._runtime_engine = lambda: engine
    shadow._schedule_restore = lambda _log: None
    shadow._schedule_persist = lambda *args, **kwargs: None
    try:
        # Engine exists, but this synchronous call has no running event loop.
        shadow.observe(
            "DOGEUSDT", _bars(), _bars(), _bars(),
            production_result=signal, min_score=60, log=log,
        )
        state = next(iter(shadow._ACTIVE.values()))
        assert state["nexus_status"] == "SCHEDULE_UNAVAILABLE"
        assert state["nexus_reason"] == "RuntimeError"
        assert shadow.snapshot()["nexus_schedule_unavailable"] == 1

        async def _retry_inside_loop():
            # Same cohort must retry instead of being discarded by _SEEN.
            shadow.observe(
                "DOGEUSDT", _bars(), _bars(), _bars(),
                production_result=signal, min_score=60, log=log,
            )
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        asyncio.run(_retry_inside_loop())
    finally:
        TradingEngine._get_market_session = original_session
        shadow.closed_mtf = original_closed
        shadow._runtime_engine = original_runtime_engine
        shadow._schedule_restore = original_schedule_restore
        shadow._schedule_persist = original_schedule_persist

    snap = shadow.snapshot()
    state = next(iter(shadow._ACTIVE.values()))
    assert engine.calls == 1
    assert snap["nexus_schedule_recovered"] == 1
    assert snap["nexus_counterfactuals"] == 1
    assert snap["nexus_approved"] == 1
    assert state["nexus_status"] == "APPROVED"
    assert any("SCHEDULE_UNAVAILABLE" in line for line in log.lines)
    assert any("SCHEDULE_RECOVERED" in line for line in log.lines)


def test_runtime_engine_lookup_uses_loaded_main_without_import_or_registration():
    sentinel = object()
    previous = sys.modules.get("main")
    sys.modules["main"] = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(engine=sentinel))
    )
    try:
        assert shadow._runtime_engine() is sentinel
    finally:
        if previous is None:
            sys.modules.pop("main", None)
        else:
            sys.modules["main"] = previous


def test_session_shadow_reads_production_policy_instead_of_copying_table():
    source = inspect.getsource(shadow)
    assert "TradingEngine._SESSION_PENALTY" in source
    assert '"DOGEUSDT": -10' not in source
    assert '"AVAXUSDT": -8' not in source


def test_scheduler_failure_is_explicit_and_recoverable():
    source = inspect.getsource(shadow)
    assert "SCHEDULE_UNAVAILABLE" in source
    assert "SCHEDULE_RECOVERED" in source
    assert "nexus_schedule_unavailable" in source
    assert "nexus_schedule_recovered" in source
    assert "except RuntimeError:\n            pass" not in source


def test_session_shadow_nexus_path_is_read_only_and_fail_closed():
    source = inspect.getsource(shadow)
    assert "engine._nexus_validate(sig)" in source
    assert "decision_validation_error" in source
    assert 'sys.modules.get("main")' in source
    forbidden = (
        "place_order", "create_order", "cancel_order", "close_position",
        "._open(", "nexus_ai.decide", "cfg.LEVERAGE =",
        "MIN_ENTRY_SCORE =", "NEXUS_MIN_SCORE =", "MIN_VOLUME_MULT =",
        "_SESSION_PENALTY =", "TradingEngine.__init__ =",
    )
    assert all(token not in source for token in forbidden)
