import asyncio
from types import SimpleNamespace

from bot.config import cfg
from bot import operator_runtime_policy as policy


class _Log:
    def warning(self, *args, **kwargs):
        return None

    def critical(self, *args, **kwargs):
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


class _EngineRaisesAfterLegacyPause:
    async def _update_balance(self):
        self.risk.drawdown = float(cfg.MAX_DRAWDOWN) + 0.01
        self.active = False
        raise RuntimeError("notification failure after legacy drawdown pause")


class _LegacyFlagAwareEngine:
    async def _update_balance(self):
        if self.risk.drawdown >= float(cfg.MAX_DRAWDOWN):
            if not getattr(self, "_dd_alerted", False):
                self._dd_alerted = True
                self.active = False


class _LateReplaceEngine:
    async def _update_balance(self):
        return None

    async def run(self):
        await self._update_balance()
        return self.active


async def _late_legacy_replacement(self):
    self.risk.drawdown = float(cfg.MAX_DRAWDOWN) + 0.01
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


def test_drawdown_advisory_restores_engine_even_when_legacy_update_raises():
    policy._install_drawdown_advisory(_EngineRaisesAfterLegacyPause, _Log())

    engine = _EngineRaisesAfterLegacyPause()
    engine.active = True
    engine.risk = SimpleNamespace(drawdown=0.0)

    try:
        asyncio.run(engine._update_balance())
    except RuntimeError as exc:
        assert "notification failure" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")

    assert engine.active is True


def test_drawdown_advisory_preempts_legacy_pause_when_already_above_threshold():
    policy._install_drawdown_advisory(_LegacyFlagAwareEngine, _Log())

    engine = _LegacyFlagAwareEngine()
    engine.active = True
    engine._dd_alerted = False
    engine.risk = SimpleNamespace(drawdown=float(cfg.MAX_DRAWDOWN) + 0.01)

    asyncio.run(engine._update_balance())

    assert engine.active is True
    assert engine._dd_alerted is True


def test_run_binds_instance_advisory_even_if_class_update_is_replaced_late():
    policy._install_drawdown_advisory(_LateReplaceEngine, _Log())

    # Reproduce a late runtime hardening/rebinding after the class-level policy.
    # The run wrapper must capture this actual method and protect the instance.
    _LateReplaceEngine._update_balance = _late_legacy_replacement

    engine = _LateReplaceEngine()
    engine.active = True
    engine.risk = SimpleNamespace(drawdown=0.0)

    result = asyncio.run(engine.run())

    assert result is True
    assert engine.active is True
    assert engine.__dict__.get("_operator_drawdown_instance_advisory") is True
