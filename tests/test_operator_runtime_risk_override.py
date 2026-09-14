import asyncio
import logging
import os
import unittest

from bot import operator_runtime_policy as policy


class _Risk:
    def __init__(self, drawdown: float):
        self.drawdown = drawdown


class _Engine:
    def __init__(self, drawdown: float):
        self.active = True
        self.risk = _Risk(drawdown)
        self._dd_alerted = False


class OperatorRuntimeRiskOverrideTests(unittest.TestCase):
    def setUp(self):
        self._previous_override = os.environ.pop(policy.RISK_OVERRIDE_ENV, None)

    def tearDown(self):
        os.environ.pop(policy.RISK_OVERRIDE_ENV, None)
        if self._previous_override is not None:
            os.environ[policy.RISK_OVERRIDE_ENV] = self._previous_override

    def test_risk_override_is_disabled_by_default(self):
        self.assertFalse(policy._risk_override_enabled())

    def test_risk_override_requires_explicit_true(self):
        os.environ[policy.RISK_OVERRIDE_ENV] = "false"
        self.assertFalse(policy._risk_override_enabled())

        os.environ[policy.RISK_OVERRIDE_ENV] = "true"
        self.assertTrue(policy._risk_override_enabled())

    def test_drawdown_pause_is_preserved_without_override(self):
        engine = _Engine(drawdown=1.0)

        async def legacy_update():
            engine.active = False

        guarded = policy._protect_drawdown_update(
            engine,
            legacy_update,
            logging.getLogger("test.operator_runtime_policy"),
            source="TEST",
        )
        asyncio.run(guarded())

        self.assertFalse(engine.active)

    def test_drawdown_pause_can_be_explicitly_overridden(self):
        engine = _Engine(drawdown=1.0)
        os.environ[policy.RISK_OVERRIDE_ENV] = "true"

        async def legacy_update():
            engine.active = False

        guarded = policy._protect_drawdown_update(
            engine,
            legacy_update,
            logging.getLogger("test.operator_runtime_policy"),
            source="TEST",
        )
        asyncio.run(guarded())

        self.assertTrue(engine.active)
        self.assertTrue(engine._dd_alerted)

    def test_operator_margin_fraction_remains_50_percent(self):
        self.assertAlmostEqual(policy.MARGIN_FRACTION, 0.50)


if __name__ == "__main__":
    unittest.main()
