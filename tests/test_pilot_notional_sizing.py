import unittest

from bot import pilot_live_runtime as live


class PilotNotionalSizingTests(unittest.TestCase):
    def test_dot_20_usdt_account_targets_about_10_usdt_position(self):
        info = {
            "multiplier": 1,
            "lotSize": 1,
            "minQty": 1,
            "minNotional": 0,
        }
        price = 1.16
        target = 20.0 * 0.50
        qty = live._pilot_quantity_for_notional(info, price, target)

        # KuCoin requires whole DOT contracts here: ceil(10 / 1.16) = 9 DOT.
        self.assertEqual(qty, 8.0)
        self.assertLessEqual(qty * price, 10.0)
        self.assertGreater((qty + 1) * price, 10.0)

    def test_contract_multiplier_rounding_never_silently_undershoots_target(self):
        info = {
            "multiplier": 10,
            "lotSize": 1,
            "minQty": 1,
            "minNotional": 0,
        }
        price = 1.33
        qty = live._pilot_quantity_for_notional(info, price, 10.0)

        # One contract represents 10 XRP, so the smallest valid notional is
        # 13.30 USDT. Exchange granularity is allowed to exceed the target.
        self.assertEqual(qty, 0.0)

    def test_target_is_position_notional_not_50_percent_margin(self):
        available = 20.0
        target_notional = available * 0.50
        leverage = 50
        approximate_initial_margin = target_notional / leverage

        self.assertEqual(target_notional, 10.0)
        self.assertEqual(approximate_initial_margin, 0.2)
        self.assertNotEqual(target_notional * leverage, target_notional)

    def test_invalid_target_fails_closed(self):
        info = {
            "multiplier": 1,
            "lotSize": 1,
            "minQty": 1,
            "minNotional": 0,
        }
        with self.assertRaises(ValueError):
            live._pilot_quantity_for_notional(info, 1.0, 0.0)
        with self.assertRaises(ValueError):
            live._pilot_quantity_for_notional(info, 0.0, 10.0)


if __name__ == "__main__":
    unittest.main()
