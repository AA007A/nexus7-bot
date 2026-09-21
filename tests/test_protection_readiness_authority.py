import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot.protection_readiness import refresh_protection_readiness
from bot.runtime_readiness import runtime_readiness


class ProtectionReadinessAuthorityTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_canonical_state_is_not_ready(self):
        engine = SimpleNamespace()
        self.assertFalse(runtime_readiness(engine).protection_system_ready)

    async def test_flat_exchange_is_ready_only_with_zero_unprotected(self):
        client = SimpleNamespace(get_positions=AsyncMock(return_value=[]))
        engine = SimpleNamespace(
            connected=True, client=client, _unprotected_symbols=set()
        )
        self.assertTrue(await refresh_protection_readiness(engine))
        self.assertTrue(engine._protection_system_ready)
        self.assertEqual(engine._protection_readiness_evidence["positions"], 0)
        self.assertEqual(
            engine._protection_readiness_evidence["unprotected_positions"], 0
        )

        engine._unprotected_symbols = {"BTCUSDT"}
        self.assertFalse(await refresh_protection_readiness(engine))
        self.assertFalse(engine._protection_system_ready)

    async def test_existing_position_requires_native_protection_readback(self):
        position = {
            "symbol": "BTCUSDT",
            "size": 1,
            "side": "Buy",
            "entryPrice": 100.0,
            "markPrice": 100.0,
            "stopLoss": 0,
        }
        client = SimpleNamespace(
            get_positions=AsyncMock(return_value=[position]),
            get_stop_orders=AsyncMock(return_value=[{
                "symbol": "XBTUSDTM",
                "side": "sell",
                "stopPrice": 95.0,
                "closeOrder": True,
                "isActive": True,
                "stopTriggered": False,
            }]),
        )
        engine = SimpleNamespace(
            connected=True, client=client, _unprotected_symbols=set()
        )
        self.assertTrue(await refresh_protection_readiness(engine))
        self.assertTrue(engine._protection_system_ready)

    async def test_inline_stop_on_wrong_side_is_not_equivalent(self):
        position = {
            "symbol": "BTCUSDT",
            "size": 1,
            "side": "Buy",
            "entryPrice": 100.0,
            "markPrice": 100.0,
            "stopLoss": 105.0,
        }
        client = SimpleNamespace(
            get_positions=AsyncMock(return_value=[position]),
        )
        engine = SimpleNamespace(
            connected=True, client=client, _unprotected_symbols=set()
        )
        self.assertFalse(await refresh_protection_readiness(engine))
        self.assertFalse(engine._protection_system_ready)

    async def test_http_success_without_readback_cannot_authorize(self):
        position = {
            "symbol": "BTCUSDT",
            "size": 1,
            "side": "Buy",
            "entryPrice": 100.0,
            "markPrice": 100.0,
            "stopLoss": 0,
        }
        client = SimpleNamespace(
            get_positions=AsyncMock(return_value=[position]),
            get_stop_orders=AsyncMock(return_value=[]),
            set_position_stops=AsyncMock(return_value=True),
        )
        engine = SimpleNamespace(
            connected=True, client=client, _unprotected_symbols=set()
        )
        self.assertFalse(await refresh_protection_readiness(engine))
        self.assertFalse(engine._protection_system_ready)
        client.set_position_stops.assert_not_awaited()

    async def test_readback_mismatch_is_false(self):
        position = {
            "symbol": "BTCUSDT",
            "size": 1,
            "side": "Buy",
            "entryPrice": 100.0,
            "markPrice": 100.0,
            "stopLoss": 0,
        }
        client = SimpleNamespace(
            get_positions=AsyncMock(return_value=[position]),
            get_stop_orders=AsyncMock(return_value=[{
                "symbol": "XBTUSDTM",
                "side": "buy",
                "stopPrice": 105.0,
                "closeOrder": True,
                "isActive": True,
                "stopTriggered": False,
            }]),
        )
        engine = SimpleNamespace(
            connected=True, client=client, _unprotected_symbols=set()
        )
        self.assertFalse(await refresh_protection_readiness(engine))
        self.assertFalse(engine._protection_system_ready)


if __name__ == "__main__":
    unittest.main()
