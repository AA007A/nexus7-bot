import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot.protection_readiness import refresh_protection_readiness
from bot.order_state import OrderRegistry, OrderState
from bot.runtime_readiness import runtime_readiness


def _registry():
    return OrderRegistry()


class ProtectionReadinessAuthorityTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_canonical_state_is_not_ready(self):
        engine = SimpleNamespace()
        self.assertFalse(runtime_readiness(engine).protection_system_ready)

    async def test_flat_exchange_is_ready_only_with_zero_unprotected(self):
        client = SimpleNamespace(
            get_positions=AsyncMock(return_value=[]),
            _get=AsyncMock(return_value={"items": []}),
        )
        engine = SimpleNamespace(
            connected=True, client=client, _unprotected_symbols=set(),
            orders=_registry(),
        )
        self.assertTrue(await refresh_protection_readiness(engine))
        self.assertTrue(engine._protection_system_ready)
        self.assertEqual(engine._protection_readiness_evidence["positions"], 0)
        self.assertEqual(
            engine._protection_readiness_evidence["unprotected_positions"], 0
        )

        # A stale incident is not itself exposure authority. Once all
        # independent flat proofs succeed it must converge and clear.
        engine._unprotected_symbols = {"BTCUSDT"}
        self.assertTrue(await refresh_protection_readiness(engine))
        self.assertEqual(engine._unprotected_symbols, set())
        self.assertTrue(engine._protection_system_ready)

    async def test_stale_unprotected_symbol_is_cleared_only_after_confirmed_flat(self):
        client = SimpleNamespace(
            get_positions=AsyncMock(return_value=[]),
            _get=AsyncMock(return_value={"items": []}),
        )
        engine = SimpleNamespace(
            connected=True, client=client, _unprotected_symbols={"SOLUSDT"},
            orders=_registry(),
        )
        self.assertTrue(await refresh_protection_readiness(engine))
        self.assertEqual(engine._unprotected_symbols, set())
        self.assertTrue(engine._protection_system_ready)

    async def test_flat_read_failure_keeps_stale_state_blocked(self):
        client = SimpleNamespace(
            get_positions=AsyncMock(return_value=[]),
            _get=AsyncMock(side_effect=TimeoutError("exchange read failed")),
        )
        engine = SimpleNamespace(
            connected=True, client=client, _unprotected_symbols={"SOLUSDT"},
            orders=_registry(),
        )
        self.assertFalse(await refresh_protection_readiness(engine))
        self.assertEqual(engine._unprotected_symbols, {"SOLUSDT"})

    async def test_flat_with_pending_durable_intent_keeps_stale_state_blocked(self):
        client = SimpleNamespace(
            get_positions=AsyncMock(return_value=[]),
            _get=AsyncMock(return_value={"items": []}),
        )
        orders = _registry()
        order, _ = orders.get_or_create("bgx7-pending", "SOLUSDT", "Buy", 1.0)
        order.transition(OrderState.SUBMITTING, source="REST")
        engine = SimpleNamespace(
            connected=True, client=client, _unprotected_symbols={"SOLUSDT"},
            orders=orders,
        )
        self.assertFalse(await refresh_protection_readiness(engine))
        self.assertEqual(engine._unprotected_symbols, {"SOLUSDT"})

    async def test_flat_with_active_entry_order_keeps_stale_state_blocked(self):
        client = SimpleNamespace(
            get_positions=AsyncMock(return_value=[]),
            _get=AsyncMock(return_value={"items": [{"symbol": "SOLUSDT", "reduceOnly": False}]}),
        )
        engine = SimpleNamespace(
            connected=True, client=client, _unprotected_symbols={"SOLUSDT"},
            orders=_registry(),
        )
        self.assertFalse(await refresh_protection_readiness(engine))
        self.assertEqual(engine._unprotected_symbols, {"SOLUSDT"})

    async def test_recent_local_fill_blocks_clear_until_exchange_converges(self):
        client = SimpleNamespace(
            get_positions=AsyncMock(return_value=[]),
            _get=AsyncMock(return_value={"items": []}),
        )
        engine = SimpleNamespace(
            connected=True, client=client, _unprotected_symbols={"SOLUSDT"},
            orders=_registry(),
            positions={"SOLUSDT": SimpleNamespace()},
        )
        self.assertFalse(await refresh_protection_readiness(engine))
        self.assertEqual(engine._unprotected_symbols, {"SOLUSDT"})
        engine.positions = {}
        self.assertTrue(await refresh_protection_readiness(engine))
        self.assertEqual(engine._unprotected_symbols, set())

    async def test_confirming_sol_flat_does_not_clear_eth_unprotected(self):
        client = SimpleNamespace(
            get_positions=AsyncMock(return_value=[]),
            _get=AsyncMock(return_value={"items": []}),
        )
        engine = SimpleNamespace(
            connected=True, client=client,
            _unprotected_symbols={"SOLUSDT", "ETHUSDT"},
            orders=_registry(),
            positions={"ETHUSDT": SimpleNamespace()},
        )
        self.assertFalse(await refresh_protection_readiness(engine))
        self.assertEqual(engine._unprotected_symbols, {"ETHUSDT"})
        self.assertFalse(engine._protection_system_ready)

    async def test_flat_reconciliation_is_idempotent(self):
        client = SimpleNamespace(
            get_positions=AsyncMock(return_value=[]),
            _get=AsyncMock(return_value={"items": []}),
        )
        engine = SimpleNamespace(
            connected=True, client=client, _unprotected_symbols={"SOLUSDT"},
            orders=_registry(), positions={},
        )
        self.assertTrue(await refresh_protection_readiness(engine))
        self.assertTrue(await refresh_protection_readiness(engine))
        self.assertEqual(engine._unprotected_symbols, set())

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
            connected=True, client=client, _unprotected_symbols=set(), orders=_registry()
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
            connected=True, client=client, _unprotected_symbols=set(), orders=_registry()
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
            connected=True, client=client, _unprotected_symbols=set(), orders=_registry()
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
            connected=True, client=client, _unprotected_symbols=set(), orders=_registry()
        )
        self.assertFalse(await refresh_protection_readiness(engine))
        self.assertFalse(engine._protection_system_ready)


if __name__ == "__main__":
    unittest.main()
