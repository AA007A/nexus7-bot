from types import SimpleNamespace
from datetime import datetime, timezone
import time
import unittest

from bot.config import cfg
from bot.professional_risk import CapitalState


class _Log:
    def __getattr__(self, _name):
        return lambda *args, **kwargs: None


class OperatorRuntimePolicyTests(unittest.TestCase):
    def test_drawdown_is_advisory_but_position_capacity_still_blocks(self):
        from bot.risk import RiskManager
        from bot.risk_manager_v3 import RiskManagerV3
        from bot import operator_runtime_policy as policy

        old_dd = cfg.MAX_DRAWDOWN
        old_max_positions = cfg.MAX_POSITIONS
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

            self.assertTrue(legacy.can_open(0))
            self.assertFalse(legacy.can_open(2))

            v3 = RiskManagerV3()
            v3.update_capital(CapitalState(equity=80.0, available_collateral=40.0))
            v3.restore_peak_equity(100.0)
            self.assertAlmostEqual(v3.drawdown, 0.20)
            self.assertTrue(v3.can_open(0))
            self.assertFalse(v3.can_open(2))
        finally:
            cfg.MAX_DRAWDOWN = old_dd
            cfg.MAX_POSITIONS = old_max_positions

    def test_live_pilot_target_is_fifty_percent_available_margin(self):
        from bot import engine as engine_module
        from bot import operator_runtime_policy as policy
        from bot import pilot_risk_cap_hardening as pilot_cap

        old_leverage = cfg.LEVERAGE
        original_minimum = engine_module.minimum_base_quantity
        original_marker = getattr(engine_module, "_operator_margin_sizing_installed", False)

        risk = SimpleNamespace(size=lambda *args, **kwargs: 0.01)
        fake_engine = SimpleNamespace(
            paper_trade=False,
            pilot=SimpleNamespace(enabled=True),
            _pilot_available_balance=20.0,
            risk=risk,
            instruments={},
            positions={},
        )
        info = {
            "multiplier": "0.01",
            "lotSize": "1",
            "minQty": "1",
            "minNotional": "0",
        }

        try:
            cfg.LEVERAGE = 50
            engine_module._operator_margin_sizing_installed = False
            policy._install_margin_sizing(_Log())
            token_engine = pilot_cap._PILOT_ENGINE.set(fake_engine)
            token_symbol = pilot_cap._PILOT_SYMBOL.set("TESTUSDT")
            token_qty = pilot_cap._PILOT_FINAL_QTY.set(None)
            try:
                qty = engine_module.minimum_base_quantity(info, 100.0)
                # available=20; 50% margin=10; 50x => target notional=500;
                # qty=5 @ $100 => $500 notional => $10 initial margin.
                self.assertAlmostEqual(qty, 5.0)
                self.assertAlmostEqual((qty * 100.0) / cfg.LEVERAGE, 10.0)
                self.assertAlmostEqual(pilot_cap._PILOT_FINAL_QTY.get(), 5.0)
            finally:
                pilot_cap._PILOT_FINAL_QTY.reset(token_qty)
                pilot_cap._PILOT_SYMBOL.reset(token_symbol)
                pilot_cap._PILOT_ENGINE.reset(token_engine)
        finally:
            cfg.LEVERAGE = old_leverage
            engine_module.minimum_base_quantity = original_minimum
            engine_module._operator_margin_sizing_installed = original_marker

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
