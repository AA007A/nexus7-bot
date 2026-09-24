"""Equity-based final loss budget.

Replaces the former ``loss_limit = margin * 0.50`` semantics, under which a
50%-margin position at 50x could lose ~25% of equity at its stop.
"""
import unittest

from bot.final_loss_budget import LossBudgetExceeded, measure, validate


class FinalLossBudgetTests(unittest.TestCase):
    def test_budget_is_equity_times_risk_pct_including_costs(self):
        # 0.5 units @100, 0.6 stop + 0.22 costs => 0.41 loss; 1% of 50 = 0.5.
        projected, ceiling = validate(0.5, 100, 99.4, 'LONG', 50, .0022, equity=50, risk_pct=0.01)
        self.assertAlmostEqual(projected, 0.41)
        self.assertAlmostEqual(ceiling, 0.5)

    def test_leverage_does_not_change_the_limit(self):
        for leverage in (1, 10, 50, 125):
            _, ceiling = validate(0.5, 100, 99.4, 'LONG', leverage, .0022, equity=50, risk_pct=0.01)
            self.assertAlmostEqual(ceiling, 0.5)

    def test_costs_can_make_previously_valid_price_stop_exceed_budget(self):
        # Price loss alone 0.45 < 0.5, with costs 0.56 > 0.5.
        with self.assertRaises(LossBudgetExceeded):
            validate(0.5, 100, 99.1, 'LONG', 50, .0022, equity=50, risk_pct=0.01)

    def test_symmetric_short_and_exact_boundary(self):
        projected, ceiling = validate(0.5, 100, 100.78, 'SHORT', 50, .0022, equity=50, risk_pct=0.01)
        self.assertAlmostEqual(projected, ceiling)

    def test_invalid_data_and_wrong_direction_fail_closed(self):
        for stop, direction in ((float('nan'), 'LONG'), (101, 'LONG'), (99, 'SHORT'), (99, 'BUY')):
            with self.assertRaises(ValueError):
                validate(0.5, 100, stop, direction, 50, .0022, equity=50, risk_pct=0.01)
        for equity, risk_pct in ((0, 0.01), (-1, 0.01), (float('nan'), 0.01), (50, 0), (50, 1.5), (None, 0.01)):
            with self.assertRaises(ValueError):
                validate(0.5, 100, 99.4, 'LONG', 50, .0022, equity=equity, risk_pct=risk_pct)

    def test_equity_is_mandatory(self):
        with self.assertRaises(TypeError):
            validate(0.5, 100, 99.4, 'LONG', 50, .0022)

    def test_production_state_old_operator_size_is_rejected(self):
        # equity 25.9007, 50% margin @50x => notional 647.5 => 6.475 units @100.
        with self.assertRaises(LossBudgetExceeded):
            validate(6.475, 100, 99.6, 'LONG', 50, .0022, equity=25.9007, risk_pct=0.01)
        m = measure(6.475, 100, 99.6, 'LONG', 50, .0022, equity=25.9007, risk_pct=0.01)
        self.assertGreater(m['projected_loss_pct_equity'], 15.0)


if __name__ == '__main__':
    unittest.main()
