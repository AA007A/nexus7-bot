import inspect
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot import shadow_live


class _Risk:
    def __init__(self):
        self.balance = 0.0
        self.drawdown = 0.0
        self._ready = False
    def init(self, balance):
        self.balance = balance
        self._ready = True
    def update(self, balance):
        self.balance = balance


class _Client:
    def __init__(self):
        self.start_private_websocket_called = False
        self._instruments = {"ETHUSDT": {"minQty": 1, "multiplier": 0.01}}
    async def ping(self):
        return True
    async def _get(self, path, params=None, auth=False):
        if path != "/api/v1/account-overview" or auth is not True:
            raise AssertionError("shadow balance path must be authenticated read-only account overview")
        return {
            "currency": "USDT",
            "accountEquity": "20.0",
            "marginBalance": "19.5",
            "availableBalance": "0.4",
            "unrealisedPNL": "0.5",
            "positionMargin": "19.6",
            "orderMargin": "0",
            "frozenFunds": "0",
        }
    async def get_balance(self):
        raise AssertionError("legacy availableBalance-only path must not drive SHADOW risk")
    async def load_instruments(self):
        return self._instruments
    def get_instruments(self):
        return self._instruments
    async def start_websocket(self, symbols, intervals):
        self.ws_symbols = list(symbols)
        self.ws_intervals = list(intervals)
    def start_private_websocket(self, *args, **kwargs):
        self.start_private_websocket_called = True
        raise AssertionError("private websocket must not start in shadow mode")


class ShadowLiveReadOnlyTests(unittest.IsolatedAsyncioTestCase):
    def test_module_has_no_exchange_mutation_calls(self):
        source = inspect.getsource(shadow_live)
        forbidden = (
            ".place_order(",
            ".cancel_all_orders(",
            ".cancel_order(",
            ".set_leverage(",
            ".set_sl(",
            ".set_position_stops(",
            ".close_position(",
        )
        for token in forbidden:
            self.assertNotIn(token, source, token)

    async def test_connect_readonly_uses_public_data_only(self):
        client = _Client()
        engine = SimpleNamespace(
            client=client,
            risk=_Risk(),
            instruments={},
            viable_symbols=[],
            connected=False,
            active=False,
        )

        async def _filter_viable_symbols():
            engine.viable_symbols = ["ETHUSDT"]
            return True

        engine._filter_viable_symbols = _filter_viable_symbols
        with patch("bot.drawdown_persistence.db.load_key_value", AsyncMock(return_value=None)), \
             patch("bot.drawdown_persistence.db.save_key_value", AsyncMock(return_value=True)):
            ok = await shadow_live.connect_readonly(engine)
        self.assertTrue(ok)
        self.assertTrue(engine.connected)
        self.assertTrue(engine.active)
        self.assertEqual(engine.risk.balance, 20.0)
        self.assertEqual(engine._shadow_available_balance, 0.4)
        self.assertEqual(client.ws_symbols, ["ETHUSDT"])
        self.assertFalse(client.start_private_websocket_called)

    async def test_failed_balance_read_fails_closed(self):
        client = _Client()
        client._get = AsyncMock(side_effect=RuntimeError("account overview unavailable"))
        engine = SimpleNamespace(
            client=client,
            risk=_Risk(),
            instruments={},
            viable_symbols=[],
            connected=True,
            active=True,
        )
        engine._filter_viable_symbols = AsyncMock(return_value=True)
        ok = await shadow_live.connect_readonly(engine)
        self.assertFalse(ok)
        self.assertFalse(engine.connected)
        self.assertFalse(engine.active)


if __name__ == "__main__":
    unittest.main()
