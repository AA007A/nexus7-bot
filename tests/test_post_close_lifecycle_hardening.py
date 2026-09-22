import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot import conditional_stop_lifecycle as lifecycle
from bot.durable_live_reconciliation import reconcile_pending
from bot.native_stop_repair import set_stops
from bot.order_state import OrderRegistry, OrderState


class DurableTerminalizationTests(unittest.IsolatedAsyncioTestCase):
    def engine(self, qty=10.0, filled=5.0):
        orders = OrderRegistry()
        order, _ = orders.get_or_create(
            "bgx7-test-partial", "ATOMUSDT", "Sell", qty
        )
        order.reduce_only = True
        order.exposure_intent = "REDUCE"
        order.previous_position_qty = 20.0
        order.transition(OrderState.SUBMITTING, source="REST")
        order.transition(OrderState.SUBMITTED, order_id="oid-1", source="REST")
        order.transition(
            OrderState.PARTIALLY_FILLED,
            order_id="oid-1",
            filled_qty=filled,
            source="WS",
        )
        client = SimpleNamespace(
            _execution_ownership=object(),
            get_order_by_client_oid=AsyncMock(),
        )
        engine = SimpleNamespace(
            paper_trade=False,
            connected=True,
            client=client,
            orders=orders,
            positions={},
            _execution_ownership_valid=True,
            _durable_state_errors=set(),
            _durable_state_ok=True,
            _durable_live_reconcile_last=0.0,
        )
        return engine, order

    async def run_reconcile(self, engine):
        with patch(
            "bot.execution_ownership.validate_execution_ownership",
            new=AsyncMock(return_value=None),
        ), patch(
            "bot.durable_execution.persist_orders",
            new=AsyncMock(return_value=True),
        ):
            return await reconcile_pending(engine, min_interval_s=0)

    async def test_lost_terminal_ws_done_recovers_partial_to_filled_without_restart(self):
        engine, order = self.engine()
        engine.client.get_order_by_client_oid.return_value = {
            "clientOid": order.client_oid,
            "orderId": "oid-1",
            "symbol": "ATOMUSDTM",
            "isActive": False,
            "cancelExist": False,
            "status": "done",
            "filledSize": 10.0,
        }
        self.assertTrue(await self.run_reconcile(engine))
        self.assertEqual(order.state, OrderState.FILLED)
        self.assertEqual(engine.orders.pending_orders(), [])

    async def test_position_flat_alone_never_terminalizes_ambiguous_order(self):
        engine, order = self.engine()
        engine.positions = {}
        engine.client.get_order_by_client_oid.return_value = {
            "clientOid": order.client_oid,
            "orderId": "oid-1",
            "symbol": "ATOMUSDTM",
            "isActive": True,
            "filledSize": 5.0,
        }
        self.assertFalse(await self.run_reconcile(engine))
        self.assertEqual(order.state, OrderState.PARTIALLY_FILLED)

    async def test_missing_active_and_terminal_status_is_fail_closed(self):
        engine, order = self.engine()
        engine.client.get_order_by_client_oid.return_value = {
            "clientOid": order.client_oid,
            "orderId": "oid-1",
            "symbol": "ATOMUSDTM",
            "filledSize": 5.0,
        }
        self.assertFalse(await self.run_reconcile(engine))
        self.assertEqual(order.state, OrderState.PARTIALLY_FILLED)

    async def test_rest_then_duplicate_ws_done_is_idempotent(self):
        engine, order = self.engine()
        engine.client.get_order_by_client_oid.return_value = {
            "clientOid": order.client_oid,
            "orderId": "oid-1",
            "symbol": "ATOMUSDTM",
            "isActive": False,
            "status": "done",
            "filledSize": 10.0,
        }
        self.assertTrue(await self.run_reconcile(engine))
        history_len = len(order.history)
        order.transition(OrderState.FILLED, filled_qty=10.0, source="WS")
        self.assertEqual(order.state, OrderState.FILLED)
        self.assertEqual(len(order.history), history_len)

    async def test_partial_execution_cancelled_remainder_is_not_full_fill(self):
        engine, order = self.engine()
        engine.client.get_order_by_client_oid.return_value = {
            "clientOid": order.client_oid,
            "orderId": "oid-1",
            "symbol": "ATOMUSDTM",
            "isActive": False,
            "cancelExist": True,
            "status": "done",
            "filledSize": 5.0,
        }
        self.assertTrue(await self.run_reconcile(engine))
        self.assertEqual(order.state, OrderState.CANCELLED)
        self.assertNotEqual(order.state, OrderState.FILLED)

    async def test_identity_mismatch_fails_closed(self):
        engine, order = self.engine()
        engine.client.get_order_by_client_oid.return_value = {
            "clientOid": "bgx7-different",
            "orderId": "oid-1",
            "symbol": "ATOMUSDTM",
            "isActive": False,
            "status": "done",
            "filledSize": 10.0,
        }
        self.assertFalse(await self.run_reconcile(engine))
        self.assertEqual(order.state, OrderState.PARTIALLY_FILLED)


class _StopClient:
    def __init__(self, orders=None, post_mode="accept"):
        self.orders = list(orders or [])
        self.post_mode = post_mode
        self.post_calls = []
        self.cancelled = []
        self._instruments = {
            "ATOMUSDT": {
                "multiplier": "0.1",
                "lotSize": "1",
                "minQty": "1",
                "tickSize": "0.001",
            }
        }

    async def get_positions(self):
        return [{
            "symbol": "ATOMUSDT",
            "size": 1574,
            "sizeUnit": "CONTRACTS",
            "side": "Buy",
            "entryPrice": 1.786,
            "markPrice": 1.800,
        }]

    def get_instruments(self):
        return self._instruments

    def _round_price(self, price, symbol):
        return str(round(float(price), 3))

    async def get_stop_orders(self, symbol):
        return list(self.orders)

    async def get_order_by_client_oid(self, client_oid):
        for row in self.orders:
            if row.get("clientOid") == client_oid:
                return dict(row)
        return {}

    async def _post(self, endpoint, body, **kwargs):
        self.post_calls.append(dict(body))
        if self.post_mode == "timeout":
            raise TimeoutError("ambiguous")
        if self.post_mode == "300004":
            raise RuntimeError("300004 stop-order quantity limit exceeded")
        if self.post_mode == "empty":
            return {}
        row = dict(body)
        row.update({
            "id": f"new-{len(self.post_calls)}",
            "isActive": True,
            "status": "open",
            "stopTriggered": False,
        })
        self.orders.append(row)
        return {"orderId": row["id"]}

    async def cancel_order_by_id(self, order_id):
        self.cancelled.append(order_id)
        self.orders = [
            row for row in self.orders
            if str(row.get("id") or row.get("orderId") or "") != str(order_id)
        ]
        return True


class _KucoinModule:
    PAPER_TRADE = False
    API_KEY = "test"

    @staticmethod
    def to_kucoin(symbol):
        return symbol + "M"


class ConditionalStopLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        lifecycle._CACHE.clear()
        lifecycle._LOCKS.clear()
        self.logger = SimpleNamespace(
            error=lambda *args, **kwargs: None,
            warning=lambda *args, **kwargs: None,
            info=lambda *args, **kwargs: None,
        )

    def stop(
        self, oid, trigger, *, client_oid=None, stop="down",
        symbol="ATOMUSDTM", external=False,
    ):
        return {
            "id": oid,
            "clientOid": (
                "external-user" if external
                else (client_oid or f"bgx-stop-{oid}")
            ),
            "symbol": symbol,
            "side": "sell",
            "stop": stop,
            "stopPrice": str(trigger),
            "stopPriceType": "MP",
            "closeOrder": True,
            "reduceOnly": True,
            "isActive": True,
            "status": "open",
            "stopTriggered": False,
            "createdAt": 1,
            "updatedAt": 1,
        }

    async def seed_verified_sl(self, client, trigger=1.775):
        lineage = lifecycle.position_lineage(
            client,
            "ATOMUSDT",
            {"side": "Buy"},
        )
        candidate = await lifecycle.prepare_candidate(
            client,
            "ATOMUSDT",
            "buy",
            "sell",
            "SL",
            lineage,
            str(round(trigger, 3)),
        )
        self.assertTrue(candidate["post_allowed"])
        old = self.stop(
            "old",
            trigger,
            client_oid=candidate["client_oid"],
        )
        client.orders.append(old)
        self.assertTrue(await lifecycle.mark_verified(
            client,
            candidate["slot_key"],
            "old",
        ))
        return old

    async def test_cross_invocation_ambiguous_repair_does_not_submit_new_order(self):
        client = _StopClient(post_mode="timeout")
        self.assertFalse(await set_stops(
            client, "ATOMUSDT", 1.790, 0, _KucoinModule, self.logger
        ))
        first_oid = client.post_calls[0]["clientOid"]
        self.assertFalse(await set_stops(
            client, "ATOMUSDT", 1.790, 0, _KucoinModule, self.logger
        ))
        self.assertEqual(len(client.post_calls), 1)
        state = lifecycle._CACHE[id(client)]
        pending = next(iter(state["slots"].values()))
        self.assertEqual(pending["client_oid"], first_oid)

    async def test_same_identity_is_reused_after_bounded_ambiguity_window(self):
        client = _StopClient(post_mode="timeout")
        self.assertFalse(await set_stops(
            client, "ATOMUSDT", 1.790, 0, _KucoinModule, self.logger
        ))
        first_oid = client.post_calls[0]["clientOid"]
        state = lifecycle._CACHE[id(client)]
        next(iter(state["slots"].values()))["attempted_at"] = 0.0
        self.assertFalse(await set_stops(
            client, "ATOMUSDT", 1.790, 0, _KucoinModule, self.logger
        ))
        self.assertEqual(len(client.post_calls), 2)
        self.assertEqual(client.post_calls[1]["clientOid"], first_oid)

    async def test_successful_replacement_verifies_new_before_selective_old_cleanup(self):
        client = _StopClient()
        old = await self.seed_verified_sl(client)
        client.orders.append(self.stop("ext", 1.770, external=True))
        self.assertTrue(await set_stops(
            client, "ATOMUSDT", 1.790, 0, _KucoinModule, self.logger
        ))
        self.assertIn(old["id"], client.cancelled)
        self.assertTrue(any(
            row.get("clientOid", "").startswith("bgx-stop-")
            and row.get("stopPrice") == "1.79"
            for row in client.orders
        ))
        self.assertTrue(any(
            row.get("clientOid") == "external-user" for row in client.orders
        ))

    async def test_failed_replacement_preserves_old_verified_protection(self):
        client = _StopClient(post_mode="timeout")
        old = await self.seed_verified_sl(client)
        self.assertFalse(await set_stops(
            client, "ATOMUSDT", 1.790, 0, _KucoinModule, self.logger
        ))
        self.assertTrue(any(row.get("id") == old["id"] for row in client.orders))
        self.assertEqual(client.cancelled, [])

    async def test_unknown_legacy_bgx_exact_stop_is_not_adopted_as_live_lineage(self):
        client = _StopClient([self.stop("legacy", 1.790)])
        self.assertTrue(await set_stops(
            client, "ATOMUSDT", 1.790, 0, _KucoinModule, self.logger
        ))
        self.assertEqual(len(client.post_calls), 1)
        self.assertNotIn("legacy", client.cancelled)

    async def test_300004_is_sticky_and_does_not_submission_storm(self):
        orders = [self.stop(str(i), 1.700 + i * 0.001) for i in range(50)]
        client = _StopClient(orders, post_mode="300004")
        self.assertFalse(await set_stops(
            client, "ATOMUSDT", 1.790, 0, _KucoinModule, self.logger
        ))
        self.assertFalse(await set_stops(
            client, "ATOMUSDT", 1.790, 0, _KucoinModule, self.logger
        ))
        self.assertEqual(len(client.post_calls), 1)
        self.assertEqual(client.cancelled, [])

    async def test_flat_cleanup_cancels_only_same_symbol_bgx_and_preserves_external_and_other_symbol(self):
        atom_bgx = self.stop("atom-bgx", 1.775)
        atom_ext = self.stop("atom-ext", 1.770, external=True)
        eth_bgx = self.stop("eth-bgx", 100.0, symbol="ETHUSDTM")
        client = _StopClient([atom_bgx, atom_ext, eth_bgx])
        engine = SimpleNamespace(client=client, positions={}, orders=OrderRegistry())
        self.assertTrue(await lifecycle.cleanup_flat_symbol(
            engine,
            "ATOMUSDT",
            exchange_position_qty=0.0,
            active_entry_confirmed_absent=True,
        ))
        self.assertIn("atom-bgx", client.cancelled)
        self.assertNotIn("eth-bgx", client.cancelled)
        self.assertTrue(any(
            row.get("clientOid") == "external-user" for row in client.orders
        ))
        self.assertTrue(any(row.get("id") == "eth-bgx" for row in client.orders))

    async def test_stop_fill_vs_cleanup_race_converges_from_final_readback(self):
        client = _StopClient([self.stop("race", 1.775)])

        async def race_cancel(order_id):
            client.orders = []
            return False

        client.cancel_order_by_id = race_cancel
        engine = SimpleNamespace(client=client, positions={}, orders=OrderRegistry())
        self.assertTrue(await lifecycle.cleanup_flat_symbol(
            engine,
            "ATOMUSDT",
            exchange_position_qty=0.0,
            active_entry_confirmed_absent=True,
        ))


class LifecycleTopologyReplayTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        lifecycle._CACHE.clear()
        lifecycle._LOCKS.clear()

    def obsolete_stop(self, oid="obsolete-1"):
        return {
            "id": oid,
            "clientOid": f"bgx-stop-{oid}",
            "symbol": "ATOMUSDTM",
            "side": "sell",
            "stop": "down",
            "stopPrice": "1.775",
            "stopPriceType": "MP",
            "closeOrder": True,
            "reduceOnly": True,
            "isActive": True,
            "status": "open",
        }

    async def reconcile(self, engine):
        with patch(
            "bot.execution_ownership.validate_execution_ownership",
            new=AsyncMock(return_value=None),
        ), patch(
            "bot.durable_execution.persist_orders",
            new=AsyncMock(return_value=True),
        ):
            return await reconcile_pending(engine, min_interval_s=0)

    async def replay_partial_then_flat(self, reason):
        orders = OrderRegistry()
        partial, _ = orders.get_or_create(
            "bgx7-partial-replay", "ATOMUSDT", "Sell", 5.0
        )
        partial.reduce_only = True
        partial.exposure_intent = "REDUCE"
        partial.previous_position_qty = 10.0
        partial.transition(OrderState.SUBMITTING, source="REST")
        partial.transition(OrderState.SUBMITTED, order_id="partial", source="REST")
        partial.transition(
            OrderState.PARTIALLY_FILLED,
            filled_qty=2.0,
            source="WS",
        )
        client = _StopClient([self.obsolete_stop()])
        client._execution_ownership = object()
        client.get_order_by_client_oid = AsyncMock(return_value={
            "clientOid": partial.client_oid,
            "orderId": "partial",
            "symbol": "ATOMUSDTM",
            "isActive": False,
            "status": "done",
            "filledSize": 5.0,
        })
        engine = SimpleNamespace(
            paper_trade=False,
            connected=True,
            client=client,
            orders=orders,
            positions={},
            _execution_ownership_valid=True,
            _durable_state_errors=set(),
            _durable_state_ok=True,
            _durable_live_reconcile_last=0.0,
        )
        self.assertTrue(await self.reconcile(engine), reason)
        self.assertTrue(await lifecycle.cleanup_flat_symbol(
            engine,
            "ATOMUSDT",
            exchange_position_qty=0.0,
            active_entry_confirmed_absent=True,
        ), reason)
        self.assertEqual(orders.pending_orders(), [], reason)
        self.assertFalse(any(
            lifecycle.is_bgx_owned(row) for row in client.orders
        ), reason)

    async def replay_full_flat(self, reason):
        client = _StopClient([self.obsolete_stop(reason.lower())])
        engine = SimpleNamespace(client=client, positions={}, orders=OrderRegistry())
        self.assertTrue(await lifecycle.cleanup_flat_symbol(
            engine,
            "ATOMUSDT",
            exchange_position_qty=0.0,
            active_entry_confirmed_absent=True,
        ), reason)
        self.assertEqual(engine.orders.pending_orders(), [], reason)
        self.assertFalse(any(
            lifecycle.is_bgx_owned(row) for row in client.orders
        ), reason)

    async def test_partial_to_final_sl(self):
        await self.replay_partial_then_flat("SL")

    async def test_partial_to_final_tp(self):
        await self.replay_partial_then_flat("TP")

    async def test_partial_to_strategy_exit(self):
        await self.replay_partial_then_flat("STRATEGY_EXIT")

    async def test_full_sl_converges_cleanly(self):
        await self.replay_full_flat("FULL_SL")

    async def test_full_tp_converges_cleanly(self):
        await self.replay_full_flat("FULL_TP")

    async def test_restart_stale_partial_and_obsolete_stops_converge_without_resubmit(self):
        source = OrderRegistry()
        order, _ = source.get_or_create(
            "bgx7-restart", "ATOMUSDT", "Sell", 5.0
        )
        order.reduce_only = True
        order.exposure_intent = "REDUCE"
        order.transition(OrderState.SUBMITTING, source="REST")
        order.transition(OrderState.SUBMITTED, order_id="restart-o", source="REST")
        order.transition(
            OrderState.PARTIALLY_FILLED,
            filled_qty=2.0,
            source="WS",
        )
        restarted = OrderRegistry()
        restarted.restore(source.snapshot())
        restored = restarted.get("bgx7-restart")
        client = _StopClient([self.obsolete_stop("legacy-stop")])
        client._execution_ownership = object()
        client.get_order_by_client_oid = AsyncMock(return_value={
            "clientOid": restored.client_oid,
            "orderId": "restart-o",
            "symbol": "ATOMUSDTM",
            "isActive": False,
            "status": "done",
            "filledSize": 5.0,
        })
        engine = SimpleNamespace(
            paper_trade=False,
            connected=True,
            client=client,
            orders=restarted,
            positions={},
            _execution_ownership_valid=True,
            _durable_state_errors=set(),
            _durable_state_ok=True,
            _durable_live_reconcile_last=0.0,
        )
        self.assertTrue(await self.reconcile(engine))
        self.assertEqual(restored.state, OrderState.FILLED)
        self.assertEqual(client.post_calls, [])
        self.assertTrue(await lifecycle.cleanup_flat_symbol(
            engine,
            "ATOMUSDT",
            exchange_position_qty=0.0,
            active_entry_confirmed_absent=True,
        ))
        self.assertFalse(any(
            lifecycle.is_bgx_owned(row) for row in client.orders
        ))


if __name__ == "__main__":
    unittest.main()
