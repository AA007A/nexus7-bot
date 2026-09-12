import asyncio
from types import SimpleNamespace

from bot.config import cfg
from bot import operator_runtime_policy as policy


class _Log:
    def warning(self, *args, **kwargs):
        return None


class _Engine:
    async def _update_balance(self):
        self.risk.drawdown = float(cfg.MAX_DRAWDOWN) + 0.01
        self._dd_alerted = True
        self.active = False


class _EngineWithoutAlertFlag:
    async def _update_balance(self):
        self.risk.drawdown = float(cfg.MAX_DRAWDOWN) + 0.01
        self._dd_alerted = False
        self.active = False


def test_drawdown_advisory_restores_only_drawdown_originated_active_state():
    policy._install_drawdown_advisory(_Engine, _Log())

    engine = _Engine()
    engine.active = True
    engine.risk = SimpleNamespace(drawdown=0.0)
    asyncio.run(engine._update_balance())
    assert engine.active is True


def test_drawdown_advisory_does_not_reenable_preexisting_inactive_engine():
    policy._install_drawdown_advisory(_Engine, _Log())

    engine = _Engine()
    engine.active = False
    engine.risk = SimpleNamespace(drawdown=0.0)
    asyncio.run(engine._update_balance())
    assert engine.active is False


def test_drawdown_advisory_does_not_depend_on_alert_telemetry_state():
    policy._install_drawdown_advisory(_EngineWithoutAlertFlag, _Log())

    engine = _EngineWithoutAlertFlag()
    engine.active = True
    engine.risk = SimpleNamespace(drawdown=0.0)
    asyncio.run(engine._update_balance())
    assert engine.active is True
