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


class _Pilot:
    enabled = True
    _exposure_capacity_patched = True


class _Engine:
    def __init__(self):
        self.positions = {}
        self.risk = SimpleNamespace(_ready=True)
        self.pilot = _Pilot()

    def _contracts_to_base_qty(self, _symbol, qty):
        return qty


class ProtectedExternalCoexistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_protected_external_is_degraded_when_pilot_capacity_policy_is_active(self):
        guard = IntegrityGuard()
        engine = _Engine()
        client = _Client([
            {
                "symbol": "XRPUSDT",
                "size": "10",
                "side": "Buy",
                "entryPrice": "2.0",
                "markPrice": "2.0",
                "stopLoss": "1.8",
            }
        ])

        state = await guard.assess(client, engine)

        self.assertIn("EXTERNAL_POSITION_PROTECTED", state.codes())
        self.assertNotIn("EXTERNAL_POSITION_UNPROTECTED", state.codes())
        self.assertEqual(state.severity, Severity.DEGRADED)
        self.assertTrue(guard.can_open_new())

    async def test_unprotected_external_still_blocks_with_policy_active(self):
        guard = IntegrityGuard()
        engine = _Engine()
        client = _Client([
            {
                "symbol": "XRPUSDT",
                "size": "10",
                "side": "Buy",
                "entryPrice": "2.0",
                "markPrice": "2.0",
                "stopLoss": "0",
            }
        ])
        client._get = AsyncMock(return_value={"items": []})

        state = await guard.assess(client, engine)

        self.assertIn("EXTERNAL_POSITION_UNPROTECTED", state.codes())
        self.assertIn("POSITION_WITHOUT_STOP", state.codes())
        self.assertEqual(state.severity, Severity.BLOCKED)
        self.assertFalse(guard.can_open_new())

    async def test_protected_external_still_blocks_without_capacity_policy(self):
        guard = IntegrityGuard()
        engine = _Engine()
        engine.pilot = SimpleNamespace(enabled=True)
        client = _Client([
            {
                "symbol": "XRPUSDT",
                "size": "10",
                "side": "Buy",
                "entryPrice": "2.0",
                "markPrice": "2.0",
                "stopLoss": "1.8",
            }
        ])

        state = await guard.assess(client, engine)

        self.assertIn("EXTERNAL_POSITION_PROTECTED", state.codes())
        self.assertEqual(state.severity, Severity.BLOCKED)
        self.assertFalse(guard.can_open_new())


if __name__ == "__main__":
    unittest.main()
