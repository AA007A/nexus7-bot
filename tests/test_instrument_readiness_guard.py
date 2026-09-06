"""Regression tests for fail-closed KuCoin instrument readiness."""
from __future__ import annotations

import asyncio

from bot import instrument_readiness_guard as guard


class _Log:
    def __init__(self):
        self.lines = []

    def info(self, *args):
        self.lines.append(("info", args))

    def critical(self, *args):
        self.lines.append(("critical", args))


class _Client:
    def __init__(self, instruments):
        self._data = instruments

    def get_instruments(self):
        return self._data


def test_snapshot_rejects_missing_invalid_and_exceptional_metadata():
    assert guard._instrument_snapshot(None) == {}
    assert guard._instrument_snapshot(_Client(None)) == {}
    assert guard._instrument_snapshot(_Client([])) == {}

    class BadClient:
        def get_instruments(self):
            raise RuntimeError("boom")

    assert guard._instrument_snapshot(BadClient()) == {}


def test_empty_instruments_block_engine_run_without_calling_original(monkeypatch=None):
    from bot.engine import TradingEngine

    original_run = TradingEngine.run
    original_status = TradingEngine.get_status
    original_flag = getattr(TradingEngine, "_instrument_readiness_guard_patched", False)
    calls = []

    async def fake_run(self, *args, **kwargs):
        calls.append(True)
        return "started"

    try:
        TradingEngine.run = fake_run
        TradingEngine.get_status = lambda self: {}
        TradingEngine._instrument_readiness_guard_patched = False
        log = _Log()
        guard.install(log)

        engine = object.__new__(TradingEngine)
        engine.client = _Client({})
        engine.active = True
        engine.connected = True

        result = asyncio.run(TradingEngine.run(engine))
        assert result is None
        assert calls == []
        assert engine.active is False
        assert engine.connected is False
        assert engine._instrument_readiness_blocked is True
        assert any(level == "critical" for level, _ in log.lines)
    finally:
        TradingEngine.run = original_run
        TradingEngine.get_status = original_status
        TradingEngine._instrument_readiness_guard_patched = original_flag


def test_nonempty_instruments_allow_original_run_and_status_is_observable():
    from bot.engine import TradingEngine

    original_run = TradingEngine.run
    original_status = TradingEngine.get_status
    original_flag = getattr(TradingEngine, "_instrument_readiness_guard_patched", False)
    calls = []

    async def fake_run(self, *args, **kwargs):
        calls.append(True)
        return "started"

    try:
        TradingEngine.run = fake_run
        TradingEngine.get_status = lambda self: {"base": True}
        TradingEngine._instrument_readiness_guard_patched = False
        log = _Log()
        guard.install(log)

        engine = object.__new__(TradingEngine)
        engine.client = _Client({"BTCUSDT": {"multiplier": 0.001}})
        engine.active = False
        engine.connected = False

        assert asyncio.run(TradingEngine.run(engine)) == "started"
        assert calls == [True]
        assert engine._instrument_readiness_blocked is False
        status = TradingEngine.get_status(engine)
        assert status["instrument_readiness"] == {
            "ready": True,
            "blocked": False,
            "instrument_count": 1,
            "fail_closed": True,
        }
    finally:
        TradingEngine.run = original_run
        TradingEngine.get_status = original_status
        TradingEngine._instrument_readiness_guard_patched = original_flag
