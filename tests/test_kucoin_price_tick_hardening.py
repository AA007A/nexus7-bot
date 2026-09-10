import math
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from bot import kucoin_price_tick_hardening as hardening


class DummyKuCoinClient:
    def __init__(self):
        self._instruments = {}

    def _round_price(self, price, symbol):
        return "legacy"


class KuCoinPriceTickHardeningTests(unittest.TestCase):
    def test_power_of_ten_tick(self):
        self.assertEqual(hardening.quantize_price_to_tick(1.2346, 0.001), "1.235")

    def test_quarter_tick(self):
        self.assertEqual(hardening.quantize_price_to_tick(1.24, 0.25), "1.25")
        self.assertEqual(hardening.quantize_price_to_tick(1.37, 0.25), "1.25")
        self.assertEqual(hardening.quantize_price_to_tick(1.38, 0.25), "1.5")

    def test_half_tick(self):
        self.assertEqual(hardening.quantize_price_to_tick(10.24, 0.5), "10")
        self.assertEqual(hardening.quantize_price_to_tick(10.25, 0.5), "10.5")

    def test_non_power_of_ten_fractional_tick(self):
        self.assertEqual(hardening.quantize_price_to_tick(1.237, 0.025), "1.225")
        self.assertEqual(hardening.quantize_price_to_tick(1.238, 0.025), "1.25")

    def test_string_inputs_do_not_expand_binary_float(self):
        self.assertEqual(hardening.quantize_price_to_tick("0.075", "0.025"), "0.075")

    def test_invalid_values_fail_closed(self):
        for price, tick in [
            (0, 0.01),
            (-1, 0.01),
            (1, 0),
            (1, -0.01),
            (math.nan, 0.01),
            (1, math.inf),
            (True, 0.01),
        ]:
            with self.subTest(price=price, tick=tick):
                with self.assertRaises(ValueError):
                    hardening.quantize_price_to_tick(price, tick)

    def test_install_replaces_rounder_without_exchange_side_effect(self):
        log = Mock()
        hardening.install(DummyKuCoinClient, log)
        client = DummyKuCoinClient()
        client._instruments = {"X": {"tickSize": 0.25}}
        self.assertEqual(client._round_price(1.24, "X"), "1.25")
        self.assertTrue(DummyKuCoinClient._price_tick_hardening_patched)

    def test_installed_rounder_fails_closed_on_invalid_metadata(self):
        log = Mock()
        hardening.install(DummyKuCoinClient, log)
        client = DummyKuCoinClient()
        client._instruments = {"X": {"tickSize": 0}}
        with self.assertRaisesRegex(ValueError, "invalid price quantization"):
            client._round_price(1.0, "X")


if __name__ == "__main__":
    unittest.main()
