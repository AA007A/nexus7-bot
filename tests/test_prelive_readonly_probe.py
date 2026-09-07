import inspect
import unittest

from bot import prelive_readonly_probe as probe


class PreliveReadonlyProbeTests(unittest.TestCase):
    def test_active_order_normalization(self):
        self.assertEqual(len(probe._active_orders([{"id": "1"}])), 1)
        self.assertEqual(len(probe._active_orders({"items": [{"id": "1"}]})), 1)
        self.assertEqual(probe._active_orders({}), [])

    def test_probe_contains_no_exchange_mutation_calls(self):
        src = inspect.getsource(probe)
        forbidden = (
            "place_order(", "cancel_all_orders(", "set_leverage(",
            "set_position_stops(", "set_sl(", "trading-stop",
            "closeOrder", "reduceOnly",
        )
        for token in forbidden:
            self.assertNotIn(token, src)

    def test_private_probe_uses_private_channel(self):
        src = inspect.getsource(probe._private_ws_probe)
        self.assertIn('/api/v1/bullet-private', src)
        self.assertIn('"privateChannel": True', src)
        self.assertIn('/contractMarket/tradeOrders:', src)


if __name__ == "__main__":
    unittest.main()
