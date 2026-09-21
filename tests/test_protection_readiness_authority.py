import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.protection_readiness import (
    protection_system_ready,
    publish_exchange_protection_state,
    refresh_protection_readiness,
)


def _engine(*, positions=None):
    return SimpleNamespace(
        client=SimpleNamespace(
            get_positions=AsyncMock(return_value=[] if positions is None else positions)
        ),
        positions={},
        _external_position_symbols=set(),
        _unprotected_symbols=set(),
        _protection_system_ready=False,
        _protection_readiness_receipt=None,
    )


class ProtectionReadinessAuthorityTests(unittest.IsolatedAsyncioTestCase):
    async def test_zero_positions_is_ready_only_from_explicit_exchange_truth(self):
        engine = _engine()
        self.assertFalse(protection_system_ready(engine))
        self.assertTrue(await refresh_protection_readiness(engine))
        self.assertTrue(protection_system_ready(engine))
        self.assertEqual(engine._protection_readiness_receipt["positions"], 0)

    async def test_existing_position_requires_confirmed_readback(self):
        position = {"symbol": "BTCUSDT", "size": 0.01}
        engine = _engine(positions=[position])
        engine.positions = {"BTCUSDT": object()}
        verifier = AsyncMock(return_value=(True, "conditional_close_order"))
        with patch("bot.protection_readiness.conditional_stop_confirmed", verifier):
            self.assertTrue(await refresh_protection_readiness(engine))
        self.assertTrue(protection_system_ready(engine))
        verifier.assert_awaited_once_with(engine.client, position)

    async def test_http_success_without_readback_cannot_authorize(self):
        engine = _engine()
        engine.positions = {"BTCUSDT": object()}
        published = publish_exchange_protection_state(
            engine,
            [{"symbol": "BTCUSDT", "size": 0.01}],
            {},
        )
        self.assertFalse(published)
        self.assertFalse(protection_system_ready(engine))

    async def test_readback_mismatch_fails_closed(self):
        engine = _engine()
        engine.positions = {"BTCUSDT": object()}
        publish_exchange_protection_state(
            engine,
            [{"symbol": "BTCUSDT", "size": 0.01}],
            {"BTCUSDT": (False, "no_full_protective_stop")},
        )
        self.assertFalse(protection_system_ready(engine))
        self.assertEqual(
            engine._protection_readiness_receipt["unprotected_positions"], 1
        )

    async def test_position_change_invalidates_old_readback(self):
        engine = _engine()
        self.assertTrue(await refresh_protection_readiness(engine))
        engine.positions["ETHUSDT"] = object()
        self.assertFalse(protection_system_ready(engine))

    async def test_malformed_receipt_fails_closed_without_raising(self):
        engine = _engine()
        engine._protection_system_ready = True
        engine._protection_readiness_receipt = {
            "observed_symbols": [],
            "positions": "not-a-count",
            "protected_positions": 0,
            "unprotected_positions": 0,
            "readback_complete": True,
        }
        self.assertFalse(protection_system_ready(engine))


if __name__ == "__main__":
    unittest.main()
