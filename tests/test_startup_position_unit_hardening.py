import ast
import inspect
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from bot import startup_position_unit_hardening as hardening


class _Engine:
    def __init__(self, rows):
        self.client = SimpleNamespace(get_positions=AsyncMock(return_value=rows))
        self.positions = {}
        self.conversions = []

    def _contracts_to_base_qty(self, symbol, contracts):
        self.conversions.append((symbol, contracts))
        multipliers = {"AVAXUSDT": 0.1, "ETHUSDT": 0.01}
        if symbol not in multipliers:
            raise ValueError("missing multiplier")
        return float(contracts) * multipliers[symbol]


class StartupPositionUnitHardeningTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.log = Mock()
        # Use a fresh subclass each test because install is intentionally idempotent.
        class Engine(_Engine):
            pass
        self.Engine = Engine
        hardening.install(self.Engine, self.log)

    async def test_explicit_base_asset_size_is_not_converted_twice(self):
        engine = self.Engine([{
            "symbol": "AVAXUSDT",
            "size": 30.0,
            "sizeUnit": "BASE_ASSET",
            "sizeContracts": 300.0,
            "side": "Sell",
            "entryPrice": 27.0,
            "markPrice": 26.8,
            "liquidationPrice": 27.8,
            "unrealisedPnl": 2.0,
        }])

        await engine._load_existing_positions()

        self.assertIn("AVAXUSDT", engine.positions)
        self.assertEqual(engine.positions["AVAXUSDT"].qty, 30.0)
        self.assertEqual(engine.positions["AVAXUSDT"].direction, "SHORT")
        self.assertEqual(engine.conversions, [])

    async def test_legacy_contract_size_is_converted_exactly_once(self):
        engine = self.Engine([{
            "symbol": "AVAXUSDT",
            "size": 300.0,
            "side": "Sell",
            "entryPrice": 27.0,
            "markPrice": 26.8,
            "liquidationPrice": 27.8,
            "unrealisedPnl": 2.0,
        }])

        await engine._load_existing_positions()

        self.assertEqual(engine.positions["AVAXUSDT"].qty, 30.0)
        self.assertEqual(engine.conversions, [("AVAXUSDT", 300.0)])

    async def test_unknown_explicit_unit_fails_closed(self):
        engine = self.Engine([{
            "symbol": "AVAXUSDT",
            "size": 30.0,
            "sizeUnit": "MYSTERY",
            "side": "Sell",
            "entryPrice": 27.0,
            "markPrice": 26.8,
            "liquidationPrice": 27.8,
        }])

        await engine._load_existing_positions()

        self.assertNotIn("AVAXUSDT", engine.positions)
        self.assertTrue(self.log.critical.called)

    async def test_invalid_entry_price_is_not_loaded(self):
        engine = self.Engine([{
            "symbol": "AVAXUSDT",
            "size": 30.0,
            "sizeUnit": "BASE_ASSET",
            "side": "Sell",
            "entryPrice": 0,
        }])

        await engine._load_existing_positions()

        self.assertEqual(engine.positions, {})

    def test_module_contains_no_exchange_mutation_calls(self):
        tree = ast.parse(inspect.getsource(hardening))
        forbidden = {
            "place_order", "cancel_order", "cancel_all_orders", "close_position",
            "set_position_stops", "set_sl", "set_leverage", "_post", "_delete",
        }
        called = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        self.assertFalse(called & forbidden, called & forbidden)


if __name__ == "__main__":
    unittest.main()
