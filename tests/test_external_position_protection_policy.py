import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot.integrity import IntegrityGuard, Severity


class ExternalPositionProtectionPolicyTests(unittest.IsolatedAsyncioTestCase):
    def _engine(self):
        return SimpleNamespace(
            positions={},
            risk=SimpleNamespace(_ready=True),
        )

    def _client(self, positions):
        return SimpleNamespace(
            get_balance=AsyncMock(return_value=100.0),
            get_positions=AsyncMock(return_value=positions),
            get_instruments=lambda: {"XRPUSDT": object()},
            _time_offset_ms=0,
            _last_ws_update=time.time(),
            _rate_limit_hits=0,
        )

    async def test_external_position_with_stop_is_classified_protected_and_still_blocks(self):
        guard = IntegrityGuard()
        positions = [{
            "symbol": "XRPUSDT",
            "size": 44,
            "side": "Buy",
            "entryPrice": "1.42875",
            "stopLoss": "1.40000",
        }]

        state = await guard.assess(self._client(positions), self._engine())
        codes = state.codes()

        self.assertIn("EXTERNAL_POSITION_PROTECTED", codes)
        self.assertNotIn("EXTERNAL_POSITION_UNPROTECTED", codes)
        self.assertNotIn("POSITION_WITHOUT_STOP", codes)
        self.assertNotIn("STATE_DIVERGENCE", codes)
        self.assertEqual(state.severity, Severity.BLOCKED)
        self.assertFalse(guard.can_open_new())

    async def test_external_position_without_stop_is_classified_unprotected(self):
        guard = IntegrityGuard()
        positions = [{
            "symbol": "XRPUSDT",
            "size": 44,
            "side": "Buy",
            "entryPrice": "1.42875",
            "stopLoss": "0",
        }]

        state = await guard.assess(self._client(positions), self._engine())
        codes = state.codes()

        self.assertIn("EXTERNAL_POSITION_UNPROTECTED", codes)
        self.assertIn("POSITION_WITHOUT_STOP", codes)
        self.assertNotIn("EXTERNAL_POSITION_PROTECTED", codes)
        self.assertNotIn("STATE_DIVERGENCE", codes)
        self.assertEqual(state.severity, Severity.BLOCKED)
        self.assertFalse(guard.can_open_new())

    async def test_invalid_stop_value_fails_closed_as_unprotected(self):
        guard = IntegrityGuard()
        positions = [{
            "symbol": "XRPUSDT",
            "size": 44,
            "side": "Buy",
            "entryPrice": "1.42875",
            "stopLoss": "not-a-number",
        }]

        state = await guard.assess(self._client(positions), self._engine())
        codes = state.codes()

        self.assertIn("EXTERNAL_POSITION_UNPROTECTED", codes)
        self.assertIn("POSITION_WITHOUT_STOP", codes)
        self.assertFalse(guard.can_open_new())


if __name__ == "__main__":
    unittest.main()
