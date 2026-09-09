import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot.integrity import IntegrityGuard, Severity


class _Client:
    def __init__(self, positions):
        self.get_balance = AsyncMock(return_value=100.0)
        self.get_positions = AsyncMock(return_value=positions)
        self._time_offset_ms = 0
        self._last_ws_update = time.time()
        self._rate_limit_hits = 0

    def get_instruments(self):
        return {"XRPUSDT": {"symbol": "XRPUSDT"}}


class _Engine:
    def __init__(self, positions=None):
        self.positions = positions or {}
        self.risk = SimpleNamespace(_ready=True)

    def _contracts_to_base_qty(self, _symbol, qty):
        return qty


class ExternalPositionIntegrityTests(unittest.IsolatedAsyncioTestCase):
    def test_exchange_only_position_is_not_state_divergence(self):
        guard = IntegrityGuard()
        engine = _Engine()
        ex_positions = [
            {
                "symbol": "XRPUSDT",
                "size": "10",
                "side": "Buy",
                "entryPrice": "2.0",
                "stopLoss": "1.8",
            }
        ]

        self.assertEqual(guard._reconcile(engine, ex_positions), [])
        self.assertEqual(guard._external_positions(engine, ex_positions), ["XRPUSDT"])

    async def test_external_protected_position_is_explicit_and_fail_closed(self):
        guard = IntegrityGuard()
        engine = _Engine()
        client = _Client([
            {
                "symbol": "XRPUSDT",
                "size": "10",
                "side": "Buy",
                "entryPrice": "2.0",
                "stopLoss": "1.8",
            }
        ])

        state = await guard.assess(client, engine)
        codes = state.codes()

        self.assertIn("EXTERNAL_POSITION_PROTECTED", codes)
        self.assertNotIn("EXTERNAL_POSITION_UNPROTECTED", codes)
        self.assertNotIn("POSITION_WITHOUT_STOP", codes)
        self.assertNotIn("STATE_DIVERGENCE", codes)
        self.assertEqual(state.severity, Severity.BLOCKED)
        self.assertFalse(guard.can_open_new())

    async def test_external_unprotected_position_keeps_stop_invariant(self):
        guard = IntegrityGuard()
        engine = _Engine()
        client = _Client([
            {
                "symbol": "XRPUSDT",
                "size": "10",
                "side": "Buy",
                "entryPrice": "2.0",
                "stopLoss": "0",
            }
        ])

        state = await guard.assess(client, engine)
        codes = state.codes()

        self.assertIn("EXTERNAL_POSITION_UNPROTECTED", codes)
        self.assertIn("POSITION_WITHOUT_STOP", codes)
        self.assertNotIn("EXTERNAL_POSITION_PROTECTED", codes)
        self.assertNotIn("STATE_DIVERGENCE", codes)
        self.assertFalse(guard.can_open_new())

    def test_managed_position_still_reconciles_normally(self):
        guard = IntegrityGuard()
        local = SimpleNamespace(qty=10.0, direction="LONG", entry=2.0)
        engine = _Engine({"XRPUSDT": local})
        ex_positions = [
            {
                "symbol": "XRPUSDT",
                "size": "10",
                "side": "Buy",
                "entryPrice": "2.0",
                "stopLoss": "1.8",
            }
        ]

        self.assertEqual(guard._external_positions(engine, ex_positions), [])
        self.assertEqual(guard._reconcile(engine, ex_positions), [])


if __name__ == "__main__":
    unittest.main()
