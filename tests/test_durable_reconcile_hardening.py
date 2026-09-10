import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot import durable_reconcile_hardening as hardening
from bot import order_state
from bot.order_state import OrderRegistry, OrderState


class _Log:
    info = staticmethod(lambda *a, **k: None)
    warning = staticmethod(lambda *a, **k: None)
    critical = staticmethod(lambda *a, **k: None)
    error = staticmethod(lambda *a, **k: None)


class DurableReconcileHardeningTests(unittest.IsolatedAsyncioTestCase):
    def _durable(self):
        async def original(engine):
            return False

        async def persist(engine, reason, strict=False):
            engine.persist_reasons.append(reason)
            return True

        def clear(engine, reason):
            engine.errors.discard(reason)
            engine._durable_state_ok = not engine.errors

        def block(engine, reason):
            engine.errors.add(reason)
            engine._durable_state_ok = False

        def advance(order, state, **info):
            if state == OrderState.FILLED and order.state == OrderState.SUBMITTING:
                order.transition(OrderState.SUBMITTED, **info)
            order.transition(state, **info)

        return SimpleNamespace(
            reconcile_orders=original,
            persist_orders=persist,
            _clear=clear,
            _block=block,
            _advance=advance,
        )

    def _engine(self):
        engine = SimpleNamespace()
        engine.orders = OrderRegistry()
        engine.errors = {"orders"}
        engine._durable_state_ok = False
        engine.persist_reasons = []
        engine.client = SimpleNamespace(get_order_status=AsyncMock(return_value={}))
        return engine

    async def test_restored_created_intent_is_terminalized_without_exchange_io(self):
        durable = self._durable()
        hardening.install(durable, order_state, _Log())
        engine = self._engine()
        order, _ = engine.orders.get_or_create("bgx7-created", "XRPUSDT", "Sell", 10.0)

        ok = await durable.reconcile_orders(engine)

        self.assertTrue(ok)
        self.assertEqual(order.state, OrderState.FAILED)
        engine.client.get_order_status.assert_not_awaited()
        self.assertTrue(engine._durable_state_ok)
        self.assertIn("startup_reconcile_hardened", engine.persist_reasons)

    async def test_order_id_recovers_filled_submitting_intent(self):
        durable = self._durable()
        hardening.install(durable, order_state, _Log())
        engine = self._engine()
        order, _ = engine.orders.get_or_create("bgx7-filled", "XRPUSDT", "Sell", 10.0)
        order.transition(OrderState.SUBMITTING, source="TEST")
        order.order_id = "kc-123"
        engine.client.get_order_status.return_value = {
            "isActive": False,
            "cancelExist": False,
            "filledSize": "1",
        }

        ok = await durable.reconcile_orders(engine)

        self.assertTrue(ok)
        self.assertEqual(order.state, OrderState.FILLED)
        self.assertEqual(order.filled_qty, 1.0)
        engine.client.get_order_status.assert_awaited_once_with("kc-123")
        self.assertTrue(engine._durable_state_ok)

    async def test_ambiguous_submitting_without_order_id_remains_fail_closed(self):
        durable = self._durable()
        hardening.install(durable, order_state, _Log())
        engine = self._engine()
        order, _ = engine.orders.get_or_create("bgx7-ambiguous", "SOLUSDT", "Buy", 1.0)
        order.transition(OrderState.SUBMITTING, source="TEST")

        ok = await durable.reconcile_orders(engine)

        self.assertFalse(ok)
        self.assertEqual(order.state, OrderState.SUBMITTING)
        self.assertFalse(engine._durable_state_ok)
        engine.client.get_order_status.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
