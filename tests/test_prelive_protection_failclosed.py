import asyncio
import types
import unittest
from unittest.mock import AsyncMock, Mock

from bot import prelive_protection_failclosed as hardening


class _FakeKuCoinModule:
    PAPER_TRADE = False
    API_KEY = "x"

    @staticmethod
    def to_kucoin(symbol):
        return symbol + "M"

    class KuCoinClient:
        _strict_position_stops_patched = False

        def _round_price(self, price, symbol):
            return str(price)


class _FakeEngine:
    _pilot_post_open_protection_patched = False

    async def _open(self, sig, *args, **kwargs):
        return "original-result"


class ProtectionFailClosedTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.log = Mock()
        self.km = _FakeKuCoinModule()
        self.km.KuCoinClient._strict_position_stops_patched = False
        _FakeEngine._pilot_post_open_protection_patched = False
        hardening.install(_FakeEngine, self.km, self.log)

    async def test_stop_verification_exception_returns_false(self):
        client = self.km.KuCoinClient()
        client._post = AsyncMock(return_value={"ok": True})
        client.get_positions = AsyncMock(side_effect=TimeoutError("offline"))
        ok = await client.set_position_stops("ETHUSDT", sl=100.0, tp=110.0)
        self.assertFalse(ok)

    async def test_open_position_without_confirmed_stop_is_marked_unprotected(self):
        engine = _FakeEngine()
        engine.paper_trade = False
        engine.pilot = types.SimpleNamespace(enabled=True)
        engine._validation_safety_lock_active = False
        engine._durable_state_enforced = False
        engine._unprotected_symbols = set()
        engine.client = types.SimpleNamespace(
            get_positions=AsyncMock(return_value=[{
                "symbol": "ETHUSDT", "size": 1, "stopLoss": 0,
            }]),
            get_stop_orders=AsyncMock(return_value=[]),
        )
        sig = types.SimpleNamespace(symbol="ETHUSDT")
        result = await engine._open(sig)
        self.assertEqual(result, "original-result")
        self.assertIn("ETHUSDT", engine._unprotected_symbols)

    async def test_native_conditional_stop_satisfies_post_open_protection(self):
        engine = _FakeEngine()
        engine.paper_trade = False
        engine.pilot = types.SimpleNamespace(enabled=True)
        engine._validation_safety_lock_active = False
        engine._durable_state_enforced = False
        engine._unprotected_symbols = {"ETHUSDT"}
        engine.client = types.SimpleNamespace(
            get_positions=AsyncMock(return_value=[{
                "symbol": "ETHUSDT", "size": 1, "stopLoss": 0,
                "side": "Buy", "entryPrice": 100.0, "markPrice": 100.0,
            }]),
            get_stop_orders=AsyncMock(return_value=[{
                "symbol": "ETHUSDTM", "side": "sell", "stopPrice": 98.0,
                "closeOrder": True, "isActive": True, "stopTriggered": False,
            }]),
        )
        sig = types.SimpleNamespace(symbol="ETHUSDT")
        result = await engine._open(sig)
        self.assertEqual(result, "original-result")
        self.assertNotIn("ETHUSDT", engine._unprotected_symbols)

    async def test_native_conditional_stop_prevents_false_emergency_close(self):
        class EngineWithGuard:
            _pilot_post_open_protection_patched = False
            _conditional_naked_guard_patched = False

            async def _open(self, sig, *args, **kwargs):
                return None

            async def _guard_naked_positions(self):
                raise AssertionError("canonical guard should be replaced")

        hardening.install(EngineWithGuard, self.km, self.log)
        engine = EngineWithGuard()
        engine.paper_trade = False
        engine.positions = {"DOTUSDT": types.SimpleNamespace(sl=1.14)}
        engine.instruments = {}
        engine._unprotected_symbols = {"DOTUSDT"}
        engine._reconcile_exchange_positions = AsyncMock()
        engine.client = types.SimpleNamespace(
            get_positions=AsyncMock(return_value=[{
                "symbol": "DOTUSDT", "size": 1, "stopLoss": 0,
                "side": "Buy", "entryPrice": 1.16, "markPrice": 1.16,
            }]),
            get_stop_orders=AsyncMock(return_value=[{
                "symbol": "DOTUSDTM", "side": "sell", "stopPrice": 1.1445,
                "closeOrder": True, "isActive": True, "stopTriggered": False,
            }]),
            set_position_stops=AsyncMock(return_value=False),
            place_order=AsyncMock(return_value={"orderId": "should-not-happen"}),
        )

        await engine._guard_naked_positions()

        engine.client.set_position_stops.assert_not_awaited()
        engine.client.place_order.assert_not_awaited()
        self.assertNotIn("DOTUSDT", engine._unprotected_symbols)

    async def test_shadow_does_not_run_post_open_exchange_reconciliation(self):
        engine = _FakeEngine()
        engine.paper_trade = False
        engine.pilot = types.SimpleNamespace(enabled=True)
        engine._validation_safety_lock_active = True
        engine._durable_state_enforced = False
        engine._unprotected_symbols = set()
        engine.client = types.SimpleNamespace(get_positions=AsyncMock())
        sig = types.SimpleNamespace(symbol="ETHUSDT")
        await engine._open(sig)
        engine.client.get_positions.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
