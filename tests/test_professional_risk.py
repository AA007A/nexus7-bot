import unittest

from bot.professional_risk import (
    CapitalState,
    capital_state_from_account_overview,
    stop_risk_size,
)


class ProfessionalRiskTests(unittest.TestCase):
    def test_wider_stop_reduces_quantity(self):
        cap = CapitalState(equity=1000, available_collateral=1000)
        common = dict(
            capital=cap,
            entry=100.0,
            risk_pct=0.01,
            leverage=10,
            qty_step=0.001,
            min_qty=0.001,
            max_margin_pct=0.8,
        )
        tight = stop_risk_size(stop=99.0, **common)
        wide = stop_risk_size(stop=96.0, **common)
        self.assertGreater(tight.qty, wide.qty)
        self.assertLessEqual(tight.projected_stop_loss, 10.00001)
        self.assertLessEqual(wide.projected_stop_loss, 10.00001)

    def test_leverage_does_not_change_risk_budget(self):
        cap = CapitalState(equity=1000, available_collateral=1000)
        base = dict(
            capital=cap,
            entry=100.0,
            stop=98.0,
            risk_pct=0.01,
            qty_step=0.001,
            min_qty=0.001,
            max_margin_pct=1.0,
        )
        low = stop_risk_size(leverage=5, **base)
        high = stop_risk_size(leverage=20, **base)
        self.assertEqual(low.risk_budget, high.risk_budget)
        self.assertAlmostEqual(low.qty, high.qty, places=6)

    def test_available_collateral_can_be_binding_constraint(self):
        cap = CapitalState(equity=1000, available_collateral=5)
        out = stop_risk_size(
            capital=cap,
            entry=100,
            stop=99,
            risk_pct=0.10,
            leverage=10,
            qty_step=0.001,
            min_qty=0.001,
            max_margin_pct=0.8,
        )
        self.assertEqual(out.binding_constraint, "AVAILABLE_COLLATERAL")
        self.assertLessEqual(out.required_margin, 4.00001)

    def test_fees_and_slippage_reduce_quantity(self):
        cap = CapitalState(equity=1000, available_collateral=1000)
        plain = stop_risk_size(
            capital=cap, entry=100, stop=99, risk_pct=0.01,
            leverage=10, qty_step=0.001, min_qty=0.001, max_margin_pct=0.8,
        )
        costed = stop_risk_size(
            capital=cap, entry=100, stop=99, risk_pct=0.01,
            leverage=10, qty_step=0.001, min_qty=0.001, max_margin_pct=0.8,
            fee_rate_per_side=0.0006, expected_slippage_pct=0.001,
        )
        self.assertLess(costed.qty, plain.qty)

    def test_minimum_order_fails_closed(self):
        cap = CapitalState(equity=10, available_collateral=10)
        out = stop_risk_size(
            capital=cap, entry=100, stop=90, risk_pct=0.001,
            leverage=2, qty_step=1.0, min_qty=1.0, max_margin_pct=0.5,
        )
        self.assertEqual(out.qty, 0.0)
        self.assertEqual(out.binding_constraint, "MINIMUM_ORDER")

    def test_account_overview_keeps_equity_available_and_margin_separate(self):
        state = capital_state_from_account_overview({
            "accountEquity": "20",
            "availableBalance": "0.25",
            "positionMargin": "19.50",
            "orderMargin": "0.25",
            "unrealisedPNL": "0.10",
        })
        self.assertEqual(state.equity, 20.0)
        self.assertEqual(state.available_collateral, 0.25)
        self.assertEqual(state.position_margin, 19.5)
        self.assertEqual(state.order_margin, 0.25)
        self.assertEqual(state.committed_margin, 19.75)

    def test_cross_margin_prefers_available_margin(self):
        state = capital_state_from_account_overview({
            "accountEquity": "100",
            "availableBalance": "12",
            "availableMargin": "65",
            "positionMargin": "30",
            "orderMargin": "5",
            "unrealisedPNL": "2",
        })
        self.assertEqual(state.available_collateral, 65.0)
        self.assertEqual(state.position_margin, 30.0)
        self.assertEqual(state.order_margin, 5.0)

    def test_negative_position_margin_is_reconstructed_without_raising(self):
        state = capital_state_from_account_overview({
            "accountEquity": "38.28",
            "availableMargin": "16.71",
            "availableBalance": "10.00",
            "positionMargin": "-1.25",
            "orderMargin": "2.00",
            "unrealisedPNL": "4.50",
        })
        self.assertEqual(state.available_collateral, 16.71)
        self.assertGreaterEqual(state.position_margin, 0.0)
        self.assertGreaterEqual(state.order_margin, 0.0)
        self.assertAlmostEqual(state.committed_margin, 21.57, places=6)

    def test_negative_order_margin_is_reconstructed_without_raising(self):
        state = capital_state_from_account_overview({
            "accountEquity": "50",
            "availableMargin": "20",
            "positionMargin": "12",
            "orderMargin": "-0.5",
        })
        self.assertEqual(state.available_collateral, 20.0)
        self.assertAlmostEqual(state.committed_margin, 30.0, places=6)


if __name__ == "__main__":
    unittest.main()
