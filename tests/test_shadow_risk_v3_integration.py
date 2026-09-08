import inspect
import unittest

from bot import shadow_live
from bot.pre_dispatch_guard import evaluate_microstructure


class _Engine:
    def __init__(self):
        self.instruments = {
            "ATOMUSDT": {"multiplier": 0.1},
        }


class ShadowRiskV3IntegrationTests(unittest.TestCase):
    def test_orderbook_contracts_are_normalized_to_base_units(self):
        raw = {
            "b": [["9.99", "100"], ["9.98", "50"]],
            "a": [["10.01", "120"], ["10.02", "80"]],
        }
        ob = shadow_live._normalize_orderbook_base_units(_Engine(), "ATOMUSDT", raw)
        self.assertEqual(ob["bids"][0], [9.99, 10.0])
        self.assertEqual(ob["asks"][0], [10.01, 12.0])

    def test_normalized_depth_can_drive_base_quantity_gate(self):
        raw = {"b": [["9.99", "100"]], "a": [["10.01", "120"]]}
        ob = shadow_live._normalize_orderbook_base_units(_Engine(), "ATOMUSDT", raw)
        result = evaluate_microstructure(
            signal_entry=10.0,
            side="BUY",
            qty=2.0,
            ticker={"bid": 9.99, "ask": 10.01, "lastPrice": 10.0},
            orderbook=ob,
        )
        self.assertTrue(result.allowed)
        self.assertGreaterEqual(result.metrics["depth_multiple"], 3.0)

    def test_missing_multiplier_fails_closed_for_depth(self):
        engine = _Engine()
        engine.instruments["ATOMUSDT"] = {}
        self.assertIsNone(
            shadow_live._normalize_orderbook_base_units(
                engine, "ATOMUSDT", {"b": [["9.99", "100"]], "a": [["10.01", "100"]]}
            )
        )

    def test_shadow_source_uses_v3_and_final_read_only_recheck(self):
        src = inspect.getsource(shadow_live.evaluate_candidate)
        self.assertIn("size_for_stop", src)
        self.assertIn("FINAL_ACCOUNT_EXPOSURE", src)
        self.assertIn("final_read_only_dispatch_recheck", src)
        self.assertIn("MICROSTRUCTURE", src)

    def test_shadow_module_contains_no_exchange_mutation_calls(self):
        src = inspect.getsource(shadow_live)
        for forbidden in (
            ".place_order(",
            ".cancel_all_orders(",
            ".set_position_stops(",
            ".set_leverage(",
            "execution_effect=SUBMIT",
            "execution_effect=MUTATE",
        ):
            self.assertNotIn(forbidden, src)


if __name__ == "__main__":
    unittest.main()
