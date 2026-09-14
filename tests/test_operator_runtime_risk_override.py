import logging

import pytest

from bot import operator_runtime_policy as policy


class _Risk:
    def __init__(self, drawdown: float):
        self.drawdown = drawdown


class _Engine:
    def __init__(self, drawdown: float):
        self.active = True
        self.risk = _Risk(drawdown)
        self._dd_alerted = False


@pytest.fixture(autouse=True)
def _clear_override(monkeypatch):
    monkeypatch.delenv(policy.RISK_OVERRIDE_ENV, raising=False)


def test_risk_override_is_disabled_by_default():
    assert policy._risk_override_enabled() is False


def test_risk_override_requires_explicit_true(monkeypatch):
    monkeypatch.setenv(policy.RISK_OVERRIDE_ENV, "false")
    assert policy._risk_override_enabled() is False

    monkeypatch.setenv(policy.RISK_OVERRIDE_ENV, "true")
    assert policy._risk_override_enabled() is True

    monkeypatch.setenv(policy.RISK_OVERRIDE_ENV, "TRUE")
    assert policy._risk_override_enabled() is True


@pytest.mark.asyncio
async def test_drawdown_pause_is_preserved_without_override():
    engine = _Engine(drawdown=1.0)

    async def legacy_update():
        engine.active = False

    guarded = policy._protect_drawdown_update(
        engine,
        legacy_update,
        logging.getLogger("test.operator_runtime_policy"),
        source="TEST",
    )
    await guarded()

    assert engine.active is False


@pytest.mark.asyncio
async def test_drawdown_pause_can_be_explicitly_overridden(monkeypatch):
    engine = _Engine(drawdown=1.0)
    monkeypatch.setenv(policy.RISK_OVERRIDE_ENV, "true")

    async def legacy_update():
        engine.active = False

    guarded = policy._protect_drawdown_update(
        engine,
        legacy_update,
        logging.getLogger("test.operator_runtime_policy"),
        source="TEST",
    )
    await guarded()

    assert engine.active is True
    assert engine._dd_alerted is True


def test_operator_margin_fraction_remains_50_percent():
    assert policy.MARGIN_FRACTION == pytest.approx(0.50)
