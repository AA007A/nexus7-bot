import asyncio

import pytest

from bot import runtime_mode_observability as rmo


class Engine:
    def __init__(self):
        self.connected = False
        self.active = False
        self.paper_trade = False
        self.viable_symbols = []
        self.risk = type("Risk", (), {"balance": 0.0, "drawdown": 0.0})()
        self.pilot = None
        self._validation_safety_lock_active = False
        self._shadow_prelive_readonly_ready = False


@pytest.mark.asyncio
async def test_deferred_notification_waits_for_runtime_readiness(monkeypatch):
    engine = Engine()
    sent = []

    async def fake_notify(message):
        sent.append(message)

    monkeypatch.setattr(rmo, "_STARTUP_READY_POLL_S", 0.001)
    monkeypatch.setattr(rmo, "_STARTUP_READY_TIMEOUT_S", 1.0)

    import bot.notifier as notifier
    monkeypatch.setattr(notifier, "notify", fake_notify)

    state = rmo.snapshot(paper_trade=False, engine=engine, blocked=False)
    first = rmo.startup_message(state)
    assert "NEXUS-7 INICIALIZANDO" in first
    assert sent == []

    await asyncio.sleep(0.005)
    assert sent == []

    engine.viable_symbols = ["BTCUSDT"]
    engine.risk.balance = 10.0
    engine.connected = True
    engine.active = True

    for _ in range(100):
        if sent:
            break
        await asyncio.sleep(0.002)

    assert len(sent) == 1
    assert "LIVE OPERACIONAL" in sent[0]
