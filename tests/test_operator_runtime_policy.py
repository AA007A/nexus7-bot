from types import SimpleNamespace
from datetime import datetime, timezone
import os
import time
import unittest

from bot.config import cfg
from bot.professional_risk import CapitalState


class _Log:
    def __getattr__(self, _name):
        return lambda *args, **kwargs: None


class OperatorRuntimePolicyTests(unittest.TestCase):
    def test_drawdown_blocks_with_and_without_override_and_capacity_still_blocks(self):
        from bot.risk import RiskManager
        from bot.risk_manager_v3 import RiskManagerV3
        from bot import operator_runtime_policy as policy

        old_dd = cfg.MAX_DRAWDOWN
        old_max_positions = cfg.MAX_POSITIONS
        previous_override = os.environ.pop(policy.RISK_OVERRIDE_ENV, None)
        try:
            policy._install_drawdown_advisory(_Log())
            cfg.MAX_DRAWDOWN = 0.10
            cfg.MAX_POSITIONS = 2

            legacy = RiskManager()
            legacy._ready = True
            legacy.balance_confirmed = True
            legacy.balance = 80.0
            legacy.peak_balance = 100.0
            legacy.drawdown = 0.20

            self.assertFalse(legacy.can_open(0))
            os.environ[policy.RISK_OVERRIDE_ENV] = "true"
            self.assertFalse(legacy.can_open(0))

            legacy.drawdown = 0.05
            self.assertTrue(legacy.can_open(0))
            self.assertFalse(legacy.can_open(2))

            v3 = RiskManagerV3()
            v3.update_capital(CapitalState(equity=80.0, available_collateral=40.0))
            v3.restore_peak_equity(100.0)
            self.assertAlmostEqual(v3.drawdown, 0.20)

            os.environ.pop(policy.RISK_OVERRIDE_ENV, None)
            self.assertFalse(v3.can_open(0))
            os.environ[policy.RISK_OVERRIDE_ENV] = "true"
            self.assertFalse(v3.can_open(0))

            v3.update_capital(CapitalState(equity=99.0, available_collateral=40.0))
            self.assertTrue(v3.can_open(0))
            self.assertFalse(v3.can_open(2))
        finally:
            cfg.MAX_DRAWDOWN = old_dd
            cfg.MAX_POSITIONS = old_max_positions
            os.environ.pop(policy.RISK_OVERRIDE_ENV, None)
            if previous_override is not None:
                os.environ[policy.RISK_OVERRIDE_ENV] = previous_override

    def test_invalid_risk_policy_blocks_new_entries(self):
        from bot.risk import RiskManager
        from bot import operator_runtime_policy as policy

        old = cfg.MAX_RISK_PCT
        try:
            policy._install_drawdown_advisory(_Log())
            legacy = RiskManager()
            legacy._ready = True
            legacy.balance_confirmed = True
            legacy.balance = 100.0
            legacy.peak_balance = 100.0
            legacy.drawdown = 0.0
            self.assertTrue(legacy.can_open(0))
            cfg.MAX_RISK_PCT = 0.30
            self.assertFalse(legacy.can_open(0))
        finally:
            cfg.MAX_RISK_PCT = old

    def test_exit_min_hold_defaults_to_ninety_minutes(self):
        from bot.exit_policy_telemetry import _min_hold_remaining

        opened = datetime.now(timezone.utc)
        pos = SimpleNamespace(opened_at=opened)
        remaining = _min_hold_remaining(pos)
        self.assertGreaterEqual(remaining, 89 * 60)
        self.assertLessEqual(remaining, 90 * 60)

    def test_exit_min_hold_expired(self):
        from bot.exit_policy_telemetry import _min_hold_remaining

        pos = SimpleNamespace(
            opened_at=time.time() - (91 * 60),
            min_hold_until=time.time() - 60,
        )
        self.assertEqual(_min_hold_remaining(pos), 0.0)


if __name__ == "__main__":
    unittest.main()
