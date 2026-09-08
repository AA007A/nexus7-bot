import inspect
import unittest

from bot import private_ws_readonly_observability as obs


class PrivateWsReadonlyObservabilityTests(unittest.TestCase):
    def test_module_contains_no_exchange_mutation_calls(self):
        src = inspect.getsource(obs)
        forbidden = (
            "place_order(", "cancel_order(", "cancel_all_orders(",
            "set_leverage(", "set_position_stops(", "set_sl(",
            "reduceOnly", "close_position(",
        )
        for token in forbidden:
            self.assertNotIn(token, src)

    def test_probe_is_diagnostic_only(self):
        src = inspect.getsource(obs.run)
        self.assertIn("_prelive_private_ws_probe_ok", src)
        self.assertIn("execution_effect=NONE", src)
        self.assertNotIn("can_open_pilot", src)
        self.assertNotIn("release_approved", src)


if __name__ == "__main__":
    unittest.main()
