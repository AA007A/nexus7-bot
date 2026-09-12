import asyncio
import inspect
from types import SimpleNamespace

from bot import session_penalty_shadow as shadow


class _Log:
    def __init__(self):
        self.events = []

    def debug(self, message, *args):
        self.events.append(("debug", message % args if args else message))

    def info(self, message, *args):
        self.events.append(("info", message % args if args else message))

    def warning(self, message, *args):
        self.events.append(("warning", message % args if args else message))


def _state():
    return {
        "symbol": "LTCUSDT",
        "direction": "LONG",
        "nexus_status": "NOT_CHECKED",
        "nexus_schedule_status": "PENDING",
        "nexus_schedule_reason": "",
    }


def test_no_running_loop_is_explicit_and_recoverable():
    key = "LTCUSDT:LONG:123"
    sig = SimpleNamespace(symbol="LTCUSDT", direction="LONG")
    log = _Log()

    old_active = shadow._ACTIVE.copy()
    old_deferred = shadow._DEFERRED_NEXUS.copy()
    old_deferred_metric = shadow._METRICS["nexus_schedule_deferred"]
    try:
        shadow._ACTIVE.clear()
        shadow._DEFERRED_NEXUS.clear()
        shadow._ACTIVE[key] = _state()

        scheduled = shadow._schedule_nexus_counterfactual(
            key, sig, [], [], [], log
        )

        assert scheduled is False
        assert key in shadow._DEFERRED_NEXUS
        assert shadow._ACTIVE[key]["nexus_schedule_status"] == "DEFERRED_NO_LOOP"
        assert shadow._ACTIVE[key]["nexus_schedule_reason"] == "no_running_loop"
        assert shadow._METRICS["nexus_schedule_deferred"] == old_deferred_metric + 1
        assert any(
            "recoverable=true" in message and "reason=no_running_loop" in message
            for level, message in log.events
            if level == "warning"
        )
    finally:
        shadow._ACTIVE.clear()
        shadow._ACTIVE.update(old_active)
        shadow._DEFERRED_NEXUS.clear()
        shadow._DEFERRED_NEXUS.update(old_deferred)
        shadow._METRICS["nexus_schedule_deferred"] = old_deferred_metric


def test_deferred_counterfactual_is_retried_when_loop_and_engine_return():
    key = "LTCUSDT:LONG:456"
    sig = SimpleNamespace(symbol="LTCUSDT", direction="LONG")
    log = _Log()

    old_active = shadow._ACTIVE.copy()
    old_deferred = shadow._DEFERRED_NEXUS.copy()
    old_runtime_engine = shadow._runtime_engine
    old_observe = shadow.observe_nexus_counterfactual
    old_recovered_metric = shadow._METRICS["nexus_schedule_recovered"]

    async def _noop_counterfactual(*args, **kwargs):
        return None

    try:
        shadow._ACTIVE.clear()
        shadow._DEFERRED_NEXUS.clear()
        shadow._ACTIVE[key] = _state()
        shadow._ACTIVE[key]["nexus_schedule_status"] = "DEFERRED_NO_LOOP"
        shadow._ACTIVE[key]["nexus_schedule_reason"] = "no_running_loop"
        shadow._DEFERRED_NEXUS[key] = (sig, [], [], [])
        shadow._runtime_engine = lambda: object()
        shadow.observe_nexus_counterfactual = _noop_counterfactual

        async def _run():
            shadow._drain_deferred_nexus(log)
            await asyncio.sleep(0)

        asyncio.run(_run())

        assert key not in shadow._DEFERRED_NEXUS
        assert shadow._ACTIVE[key]["nexus_schedule_status"] == "RECOVERED_SCHEDULED"
        assert shadow._ACTIVE[key]["nexus_schedule_reason"] == ""
        assert shadow._METRICS["nexus_schedule_recovered"] == old_recovered_metric + 1
    finally:
        shadow._ACTIVE.clear()
        shadow._ACTIVE.update(old_active)
        shadow._DEFERRED_NEXUS.clear()
        shadow._DEFERRED_NEXUS.update(old_deferred)
        shadow._runtime_engine = old_runtime_engine
        shadow.observe_nexus_counterfactual = old_observe
        shadow._METRICS["nexus_schedule_recovered"] = old_recovered_metric


def test_shadow_source_has_no_silent_runtimeerror_pass():
    source = inspect.getsource(shadow)
    assert "except RuntimeError:\n            pass" not in source
    assert "SESSION_PENALTY_NEXUS_SCHEDULE" in source
    assert "DEFERRED_NO_LOOP" in source
    assert "RECOVERED_SCHEDULED" in source


def test_shadow_scheduling_does_not_mutate_trading_controls():
    source = inspect.getsource(shadow)
    forbidden = (
        "create_order(",
        "place_order(",
        "cancel_order(",
        "set_leverage(",
        "cfg.MIN_SCORE =",
        "cfg.MIN_RR_RATIO =",
        "NEXUS_MIN_RR_NET =",
        "LIVE_TRADING_CONFIRMED =",
    )
    for token in forbidden:
        assert token not in source


def test_snapshot_exposes_schedule_health():
    snap = shadow.snapshot()
    assert "deferred_nexus" in snap
    assert "nexus_schedule_deferred" in snap
    assert "nexus_schedule_recovered" in snap
    assert "nexus_schedule_error" in snap
