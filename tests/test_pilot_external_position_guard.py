import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from bot import pilot_external_position_guard as guard


class DummyEngine:
    async def _guard_naked_positions(self):
        return "guard-original"

    async def _sync_positions(self):
        return "sync-original"

    async def _reconcile_exchange_positions(self, only_symbol=None):
        return [f"reconcile:{only_symbol}"]


class PilotExternalPositionGuardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.log = Mock()
        guard.install(DummyEngine, self.log)
        self.engine = DummyEngine()
        self.engine.paper_trade = False
        self.engine.pilot = SimpleNamespace(enabled=True)
        self.engine.positions = {}
        self.engine._unprotected_symbols = set()
        self.engine._validation_safety_lock_active = False
        self.engine.client = SimpleNamespace(
            get_positions=AsyncMock(return_value=[
                {"symbol": "ADAUSDT", "size": 10, "side": "Buy"}
            ])
        )

    async def test_pilot_guard_does_not_touch_unexpected_position(self):
        result = await self.engine._guard_naked_positions()
        self.assertIsNone(result)
        self.assertIn("ADAUSDT", self.engine._unprotected_symbols)
        self.assertTrue(self.engine._pilot_external_position_guard_blocked)

    async def test_pilot_sync_does_not_auto_adopt_external_position(self):
        result = await self.engine._sync_positions()
        self.assertIsNone(result)
        self.assertNotIn("ADAUSDT", self.engine.positions)
        self.assertIn("ADAUSDT", self.engine._unprotected_symbols)

    async def test_broad_reconcile_does_not_auto_adopt_external_position(self):
        result = await self.engine._reconcile_exchange_positions()
        self.assertIn("ADAUSDT", result)
        self.assertNotIn("ADAUSDT", self.engine.positions)

    async def test_symbol_scoped_ambiguous_fill_reconcile_is_preserved(self):
        result = await self.engine._reconcile_exchange_positions(only_symbol="ADAUSDT")
        self.assertEqual(result, ["reconcile:ADAUSDT"])

    async def test_clean_account_preserves_normal_pilot_management(self):
        self.engine.client.get_positions.return_value = []
        self.assertEqual(await self.engine._guard_naked_positions(), "guard-original")
        self.assertEqual(await self.engine._sync_positions(), "sync-original")
        self.assertEqual(await self.engine._reconcile_exchange_positions(), ["reconcile:None"])

    async def test_position_read_failure_is_fail_closed(self):
        self.engine.client.get_positions.side_effect = RuntimeError("offline")
        self.assertIsNone(await self.engine._guard_naked_positions())
        self.assertIsNone(await self.engine._sync_positions())
        self.assertTrue(self.engine._pilot_external_position_guard_blocked)


if __name__ == "__main__":
    unittest.main()
