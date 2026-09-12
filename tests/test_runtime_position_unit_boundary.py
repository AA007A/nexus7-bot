import unittest

from bot.kucoin_position_units import KuCoinPositionUnitAdapter
from bot.nexus_runtime_engine import TradingEngine


class _RawClient:
    pass


class RuntimePositionUnitBoundaryTests(unittest.TestCase):
    def test_adapter_normalized_base_quantity_is_identity_at_legacy_helper(self):
        engine = object.__new__(TradingEngine)
        engine.client = KuCoinPositionUnitAdapter(_RawClient())
        self.assertEqual(engine._contracts_to_base_qty("ETHUSDT", 0.21), 0.21)

    def test_zero_normalized_quantity_is_preserved(self):
        engine = object.__new__(TradingEngine)
        engine.client = KuCoinPositionUnitAdapter(_RawClient())
        self.assertEqual(engine._contracts_to_base_qty("ETHUSDT", 0.0), 0.0)

    def test_nan_normalized_quantity_fails_closed(self):
        engine = object.__new__(TradingEngine)
        engine.client = KuCoinPositionUnitAdapter(_RawClient())
        with self.assertRaises(ValueError):
            engine._contracts_to_base_qty("ETHUSDT", float("nan"))

    def test_negative_normalized_quantity_fails_closed(self):
        engine = object.__new__(TradingEngine)
        engine.client = KuCoinPositionUnitAdapter(_RawClient())
        with self.assertRaises(ValueError):
            engine._contracts_to_base_qty("ETHUSDT", -0.21)


if __name__ == "__main__":
    unittest.main()
