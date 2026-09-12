import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from bot.integrity import IntegrityGuard


class ReconcileBaseUnitsTests(unittest.TestCase):
    def check_quantity(self, size, unit=None):
        engine = SimpleNamespace(
            positions={"ETHUSDT": SimpleNamespace(qty=0.01, direction="LONG", entry=2531.41)},
            _contracts_to_base_qty=Mock(side_effect=lambda symbol, qty: qty * 0.01),
        )
        row = {"symbol": "ETHUSDT", "size": size, "side": "Buy", "entryPrice": 2531.41}
        if unit:
            row["sizeUnit"] = unit
        return IntegrityGuard()._reconcile(engine, [row]), engine

    def test_normalized_eth_is_not_converted_twice(self):
        issues, engine = self.check_quantity(0.01, "BASE_ASSET")
        self.assertEqual(issues, [])
        engine._contracts_to_base_qty.assert_not_called()

    def test_legacy_contracts_still_convert(self):
        issues, engine = self.check_quantity(1)
        self.assertEqual(issues, [])
        engine._contracts_to_base_qty.assert_called_once_with("ETHUSDT", 1.0)

    def test_real_normalized_divergence_still_blocks(self):
        issues, _ = self.check_quantity(0.02, "BASE_ASSET")
        self.assertTrue(any("qty local" in issue for issue in issues))
