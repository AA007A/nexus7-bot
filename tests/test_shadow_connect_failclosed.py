import unittest
from unittest.mock import AsyncMock, patch

from bot import shadow_connect
from bot.professional_risk import CapitalState


class _CapitalSnapshot:
    def __init__(self):
        self.capital = CapitalState(
            equity=100.0,
            available_collateral=50.0,
            position_margin=10.0,
            order_margin=0.0,
            unrealized_pnl=0.0,
        )


class _Client:
    def __init__(self):
        self.ping = AsyncMock(return_value=True)
        self.load_instruments = AsyncMock(return_value={"BTCUSDT": {"multiplier": 0.001}})
        self.start_websocket = AsyncMock(return_value=True)
        self._instruments = {"BTCUSDT": {"multiplier": 0.001}}

    def get_instruments(self):
        return self._instruments


class _Engine:
    def __init__(self):
        self.client = _Client()
        self.connected = False
        self.active = False
        self.instruments = {}
        self.viable_symbols = []
        self._filter_viable_symbols = AsyncMock(side_effect=self._viable)

    async def _viable(self):
        self.viable_symbols = ["BTCUSDT"]
        return True


class ShadowConnectFailClosedTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_marks_engine_ready(self):
        engine = _Engine()
        with patch.object(
            shadow_connect.balance_semantics,
            "refresh_shadow_risk",
            AsyncMock(return_value={"equity": 100.0, "available": 50.0}),
        ), patch.object(
            shadow_connect,
            "read_account_capital",
            AsyncMock(return_value=_CapitalSnapshot()),
        ):
            ok = await shadow_connect.connect_readonly(engine)

        self.assertTrue(ok)
        self.assertTrue(engine.connected)
        self.assertTrue(engine.active)
        self.assertEqual(engine.viable_symbols, ["BTCUSDT"])

    async def test_capital_error_blocks_without_ws(self):
        engine = _Engine()
        with patch.object(
            shadow_connect.balance_semantics,
            "refresh_shadow_risk",
            AsyncMock(return_value={"equity": 100.0, "available": 50.0}),
        ), patch.object(
            shadow_connect,
            "read_account_capital",
            AsyncMock(side_effect=ValueError("bad live account payload")),
        ):
            ok = await shadow_connect.connect_readonly(engine)

        self.assertFalse(ok)
        self.assertFalse(engine.connected)
        self.assertFalse(engine.active)
        engine.client.start_websocket.assert_not_awaited()

    async def test_viability_cannot_pass_with_unknown_instrument(self):
        engine = _Engine()

        async def _bad_viability():
            engine.viable_symbols = ["UNKNOWNUSDT"]
            return True

        engine._filter_viable_symbols = AsyncMock(side_effect=_bad_viability)
        with patch.object(
            shadow_connect.balance_semantics,
            "refresh_shadow_risk",
            AsyncMock(return_value={"equity": 100.0, "available": 50.0}),
        ), patch.object(
            shadow_connect,
            "read_account_capital",
            AsyncMock(return_value=_CapitalSnapshot()),
        ):
            ok = await shadow_connect.connect_readonly(engine)

        self.assertFalse(ok)
        self.assertEqual(engine.viable_symbols, [])
        self.assertFalse(engine.connected)
        engine.client.start_websocket.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
