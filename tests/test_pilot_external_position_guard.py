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
    async def test_external_position_does_not_skip_owned_management(self):
        self.engine.positions = {'LTCUSDT': SimpleNamespace(qty=7.8, direction='LONG')}
        self.engine.client.get_positions.return_value = [
            dict(symbol='LTCUSDT', size=7.8, sizeUnit='BASE_ASSET', side='Buy', stopLoss=0),
            dict(symbol='ADAUSDT', size=10, side='Buy', stopLoss=.5),
        ]
        self.assertEqual(await self.engine._guard_naked_positions(), 'guard-original')
        self.assertEqual(await self.engine._sync_positions(), 'sync-original')
        self.assertNotIn('ADAUSDT', self.engine.positions)

    async def test_external_increase_of_owned_symbol_is_quarantined(self):
        self.engine.positions = {"ADAUSDT": SimpleNamespace(qty=30., direction="LONG")}
        self.engine.client.get_positions.return_value = [dict(symbol="ADAUSDT", size=39.5,
            sizeUnit="BASE_ASSET", side="Buy", entryPrice=7.4, stopLoss=0)]
        self.engine.client.get_stop_orders = AsyncMock(return_value=[])
        self.assertIsNone(await self.engine._guard_naked_positions())
        self.assertNotIn("ADAUSDT", self.engine.positions)
        self.assertIn("ADAUSDT", self.engine._external_position_symbols)
        self.assertTrue(self.engine._pilot_external_position_guard_blocked)
        self.assertIsNone(await self.engine._reconcile_exchange_positions(only_symbol="ADAUSDT"))

    async def test_same_quantity_normalized_position_remains_owned(self):
        self.engine.positions = {"ADAUSDT": SimpleNamespace(qty=30., direction="LONG")}
        self.engine.client.get_positions.return_value = [dict(symbol="ADAUSDT", size=30.,
            sizeUnit="BASE_ASSET", side="Buy", entryPrice=7.4, stopLoss=7.3)]
        self.assertEqual(await self.engine._guard_naked_positions(), "guard-original")
        self.assertIn("ADAUSDT", self.engine.positions)

    async def test_direction_reversal_is_not_implicitly_owned(self):
        self.engine.positions = {"ADAUSDT": SimpleNamespace(qty=30., direction="LONG")}
        self.engine.client.get_positions.return_value = [dict(symbol="ADAUSDT", size=30.,
            sizeUnit="BASE_ASSET", side="Sell", entryPrice=7.4, stopLoss=7.5)]
        self.assertIsNone(await self.engine._guard_naked_positions())
        self.assertNotIn("ADAUSDT", self.engine.positions)
        self.assertFalse(self.engine._pilot_external_position_guard_blocked)

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
                {"symbol": "ADAUSDT", "size": 10, "side": "Buy", "stopLoss": 0}
            ])
        )

    async def test_pilot_guard_does_not_touch_unprotected_unexpected_position(self):
        result = await self.engine._guard_naked_positions()
        self.assertIsNone(result)
        self.assertIn("ADAUSDT", self.engine._unprotected_symbols)
        self.assertTrue(self.engine._pilot_external_position_guard_blocked)

    async def test_pilot_sync_does_not_auto_adopt_external_position(self):
        result = await self.engine._sync_positions()
        self.assertIsNone(result)
        self.assertNotIn("ADAUSDT", self.engine.positions)
        self.assertIn("ADAUSDT", self.engine._unprotected_symbols)

    async def test_broad_reconcile_reports_unprotected_external_position(self):
        result = await self.engine._reconcile_exchange_positions()
        self.assertIn("ADAUSDT", result)
        self.assertNotIn("ADAUSDT", self.engine.positions)

    async def test_protected_external_position_is_not_globally_blocked(self):
        self.engine._unprotected_symbols.add("ADAUSDT")
        self.engine.client.get_positions.return_value = [
            {"symbol": "ADAUSDT", "size": 10, "side": "Buy", "stopLoss": 0.55}
        ]
        result = await self.engine._guard_naked_positions()
        self.assertIsNone(result)
        self.assertFalse(self.engine._pilot_external_position_guard_blocked)
        self.assertNotIn("ADAUSDT", self.engine._unprotected_symbols)
        self.assertNotIn("ADAUSDT", self.engine.positions)

    async def test_protected_external_position_is_never_auto_adopted(self):
        self.engine.client.get_positions.return_value = [
            {"symbol": "ADAUSDT", "size": 10, "side": "Buy", "stopLoss": 0.55}
        ]
        self.assertIsNone(await self.engine._sync_positions())
        self.assertNotIn("ADAUSDT", self.engine.positions)
        self.assertFalse(self.engine._pilot_external_position_guard_blocked)

    async def test_broad_reconcile_does_not_report_protected_as_unprotected(self):
        self.engine.client.get_positions.return_value = [
            {"symbol": "ADAUSDT", "size": 10, "side": "Buy", "stopLoss": 0.55}
        ]
        result = await self.engine._reconcile_exchange_positions()
        self.assertEqual(result, [])
        self.assertNotIn("ADAUSDT", self.engine.positions)
        self.assertFalse(self.engine._pilot_external_position_guard_blocked)

    async def test_invalid_stop_is_fail_closed(self):
        self.engine.client.get_positions.return_value = [
            {"symbol": "ADAUSDT", "size": 10, "side": "Buy", "stopLoss": "nan"}
        ]
        await self.engine._guard_naked_positions()
        self.assertTrue(self.engine._pilot_external_position_guard_blocked)
        self.assertIn("ADAUSDT", self.engine._unprotected_symbols)

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

    async def test_shadow_external_position_is_immutable(self):
        self.engine._validation_safety_lock_active = True
        self.engine.pilot = SimpleNamespace(enabled=True)
        self.assertIsNone(await self.engine._guard_naked_positions())
        self.assertIsNone(await self.engine._sync_positions())
        self.assertNotIn("ADAUSDT", self.engine.positions)
        self.assertIn("ADAUSDT", self.engine._unprotected_symbols)

    async def test_nonpilot_live_external_position_is_immutable(self):
        self.engine._validation_safety_lock_active = False
        self.engine.pilot = SimpleNamespace(enabled=False)
        self.assertIsNone(await self.engine._guard_naked_positions())
        self.assertIsNone(await self.engine._sync_positions())
        self.assertNotIn("ADAUSDT", self.engine.positions)
        self.assertIn("ADAUSDT", self.engine._unprotected_symbols)

    async def test_paper_keeps_original_lifecycle(self):
        self.engine.paper_trade = True
        self.assertEqual(await self.engine._guard_naked_positions(), "guard-original")
        self.assertEqual(await self.engine._sync_positions(), "sync-original")
        self.assertEqual(await self.engine._reconcile_exchange_positions(), ["reconcile:None"])


if __name__ == "__main__":
    unittest.main()
