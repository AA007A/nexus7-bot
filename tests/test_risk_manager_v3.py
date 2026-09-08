import unittest

from bot.professional_risk import CapitalState
from bot.risk_manager_v3 import RiskManagerV3


class RiskManagerV3Tests(unittest.TestCase):
    def _instrument(self):
        return {
            "ATOMUSDT": {
                "multiplier": 0.1,
                "lotSize": 1,
                "minQty": 1,
                "minNotional": 0,
            }
        }

    def test_drawdown_uses_equity_not_available_collateral(self):
        rm = RiskManagerV3()
        rm.update_capital(CapitalState(1000, 1000))
        rm.update_capital(CapitalState(900, 50, position_margin=850))
        self.assertAlmostEqual(rm.drawdown, 0.10)
        self.assertEqual(rm.available_collateral, 50)

    def test_available_collateral_can_block_without_faking_drawdown(self):
        rm = RiskManagerV3()
        rm.update_capital(CapitalState(1000, 0, position_margin=1000))
        self.assertAlmostEqual(rm.drawdown, 0.0)
        self.assertFalse(rm.can_open(0))

    def test_stop_risk_budget_comes_from_equity(self):
        rm = RiskManagerV3()
        rm.update_capital(CapitalState(1000, 500))
        result = rm.size_for_stop(
            symbol="ATOMUSDT",
            entry=10,
            stop=9,
            instruments=self._instrument(),
            risk_pct=0.01,
            leverage=10,
            max_margin_pct=0.8,
            fee_rate_per_side=0,
            expected_slippage_pct=0,
        )
        self.assertAlmostEqual(result.risk_budget, 10.0)
        self.assertLessEqual(result.projected_stop_loss, 10.0 + 1e-9)

    def test_leverage_does_not_change_risk_budget(self):
        rm = RiskManagerV3()
        rm.update_capital(CapitalState(1000, 1000))
        low = rm.size_for_stop(
            symbol="ATOMUSDT", entry=10, stop=9, instruments=self._instrument(),
            risk_pct=0.01, leverage=2, max_margin_pct=0.8,
        )
        high = rm.size_for_stop(
            symbol="ATOMUSDT", entry=10, stop=9, instruments=self._instrument(),
            risk_pct=0.01, leverage=20, max_margin_pct=0.8,
        )
        self.assertAlmostEqual(low.risk_budget, high.risk_budget)
        self.assertLessEqual(low.projected_stop_loss, low.risk_budget + 1e-9)
        self.assertLessEqual(high.projected_stop_loss, high.risk_budget + 1e-9)

    def test_minimum_order_fails_closed(self):
        rm = RiskManagerV3()
        rm.update_capital(CapitalState(1, 1))
        result = rm.size_for_stop(
            symbol="ATOMUSDT",
            entry=100,
            stop=90,
            instruments=self._instrument(),
            risk_pct=0.001,
            leverage=1,
            max_margin_pct=0.1,
        )
        self.assertEqual(result.qty, 0.0)
        self.assertEqual(result.binding_constraint, "MINIMUM_ORDER")

    def test_unconfirmed_state_is_fail_closed(self):
        rm = RiskManagerV3()
        self.assertFalse(rm.can_open(0))
        with self.assertRaises(RuntimeError):
            rm.size_for_stop(
                symbol="ATOMUSDT", entry=10, stop=9,
                instruments=self._instrument(),
            )


if __name__ == "__main__":
    unittest.main()
