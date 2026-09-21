import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot.protection_readiness import refresh_protection_readiness
from bot.runtime_readiness import runtime_readiness


class Client:
    def __init__(self, positions=None, stops=None):
        self._positions = list(positions or [])
        self._stops = list(stops or [])
        self.get_positions = AsyncMock(side_effect=self._read_positions)
        self.get_stop_orders = AsyncMock(side_effect=self._read_stops)

    async def _read_positions(self):
        return list(self._positions)

    async def _read_stops(self, _symbol):
        return list(self._stops)

    def get_instruments(self):
        return {
            "BTCUSDT": {"tickSize": "0.1", "multiplier": "0.001", "lotSize": "1"},
        }


def engine(client, *, positions=None, unprotected=None, external=None):
    return SimpleNamespace(
        client=client,
        positions=dict(positions or {}),
        _unprotected_symbols=set(unprotected or set()),
        _external_position_symbols=set(external or set()),
        _protection_system_ready=False,
    )


class ProtectionReadinessAuthorityTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_protection_attribute_is_not_ready(self):
        snap = runtime_readiness(SimpleNamespace())
        self.assertFalse(snap.protection_system_ready)

    async def test_zero_positions_requires_exchange_read_and_zero_unprotected(self):
        e = engine(Client())
        self.assertTrue(await refresh_protection_readiness(e))
        self.assertTrue(e._protection_system_ready)
        e._unprotected_symbols.add("BTCUSDT")
        self.assertFalse(await refresh_protection_readiness(e))
        self.assertFalse(e._protection_system_ready)

    async def test_position_with_exact_inline_stop_is_ready(self):
        row = {
            "symbol": "BTCUSDT", "size": "10", "sizeUnit": "CONTRACTS",
            "side": "Buy", "markPrice": "101", "entryPrice": "100",
            "stopLoss": "98.0",
        }
        local = SimpleNamespace(direction="LONG", sl=98.0)
        e = engine(Client([row]), positions={"BTCUSDT": local})
        self.assertTrue(await refresh_protection_readiness(e))

    async def test_inline_stop_mismatch_is_not_ready_without_matching_readback(self):
        row = {
            "symbol": "BTCUSDT", "size": "10", "sizeUnit": "CONTRACTS",
            "side": "Buy", "markPrice": "101", "entryPrice": "100",
            "stopLoss": "97.0",
        }
        local = SimpleNamespace(direction="LONG", sl=98.0)
        e = engine(Client([row], []), positions={"BTCUSDT": local})
        self.assertFalse(await refresh_protection_readiness(e))

    async def test_exact_conditional_stop_readback_is_ready(self):
        row = {
            "symbol": "BTCUSDT", "size": "10", "sizeUnit": "CONTRACTS",
            "side": "Buy", "markPrice": "101", "entryPrice": "100",
            "stopLoss": "0",
        }
        stop = {
            "symbol": "XBTUSDTM", "side": "sell", "stop": "down",
            "stopPrice": "98.0", "stopPriceType": "MP", "closeOrder": True,
            "reduceOnly": True, "isActive": True, "stopTriggered": False,
        }
        local = SimpleNamespace(direction="LONG", sl=98.0)
        e = engine(Client([row], [stop]), positions={"BTCUSDT": local})
        self.assertTrue(await refresh_protection_readiness(e))

    async def test_http_success_without_readback_never_authorizes(self):
        row = {
            "symbol": "BTCUSDT", "size": "10", "sizeUnit": "CONTRACTS",
            "side": "Buy", "markPrice": "101", "entryPrice": "100",
            "stopLoss": "0",
        }
        local = SimpleNamespace(direction="LONG", sl=98.0)
        client = Client([row], [])
        client.last_stop_submit_http_success = True
        e = engine(client, positions={"BTCUSDT": local})
        self.assertFalse(await refresh_protection_readiness(e))
        self.assertFalse(e._protection_system_ready)

if __name__ == "__main__":
    unittest.main()
