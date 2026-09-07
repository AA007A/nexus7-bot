import inspect
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot import shadow_live


class _Risk:
    def __init__(self):
        self.balance = 0.0
    def init(self, balance):
        self.balance = balance
    def update(self, balance):
        self.balance = balance


class _Client:
    def __init__(self):
        self.start_private_websocket_called = False
        self._instruments = {"ETHUSDT": {"minQty": 1, "multiplier": 0.01}}
    async def ping(self):
        return True
    async def get_balance(self):
        return 20.0
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
        ok = await shadow_live.connect_readonly(engine)
        self.assertTrue(ok)
        self.assertTrue(engine.connected)
        self.assertTrue(engine.active)
        self.assertEqual(engine.risk.balance, 20.0)
        self.assertEqual(client.ws_symbols, ["ETHUSDT"])
        self.assertFalse(client.start_private_websocket_called)

    async def test_failed_balance_read_fails_closed(self):
        client = _Client()
        client.get_balance = AsyncMock(return_value=-1.0)
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
