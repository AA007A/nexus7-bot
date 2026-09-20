import unittest
from bot.final_loss_budget import validate


class FinalLossBudgetTests(unittest.TestCase):
    def test_budget_includes_costs_and_preserves_margin_semantics(self):
        # 20 available -> 10 margin -> 500 notional at 50x; 5 units @100.
        projected, ceiling = validate(5, 100, 99.4, 'LONG', 50, .0022)
        self.assertAlmostEqual(projected, 4.1)
        self.assertEqual(ceiling, 5)

    def test_costs_can_make_previously_valid_price_stop_exceed_budget(self):
        with self.assertRaises(ValueError):
            validate(5, 100, 99.1, 'LONG', 50, .0022)

    def test_symmetric_short_and_exact_boundary(self):
        self.assertAlmostEqual(validate(5, 100, 100.78, 'SHORT', 50, .0022)[0], 5)

    def test_invalid_data_and_wrong_direction_fail_closed(self):
        for stop, direction in ((float('nan'), 'LONG'), (101, 'LONG'), (99, 'SHORT'), (99, 'BUY')):
            with self.assertRaises(ValueError):
                validate(5, 100, stop, direction, 50, .0022)

    def test_cap_cannot_be_avoided_by_small_quantity(self):
        for qty in (.001, 1, 1000):
            with self.assertRaises(ValueError):
                validate(qty, 100, 98, 'LONG', 50, .0022)
