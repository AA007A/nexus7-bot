import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot import database as db
from bot import durable_execution as durable
from bot.engine import Position
from bot.order_state import OrderRegistry, OrderState
from bot.strategy import Signal


class _Risk:
    def __init__(self, balance=20.0, peak=20.0):
        self.balance = balance
        self.peak_balance = peak
        self.drawdown = 0.0
        self.balance_confirmed = balance > 0


def _engine(paper=True):
    return SimpleNamespace(
        paper_trade=paper,
        orders=OrderRegistry(),
        positions={},
        _trade_ids={},
        _cooldown={},
        risk=_Risk(),
        connected=True,
    )


def _position():
    signal = Signal(
        "BTCUSDT", "LONG", 100.0, 98.0, 104.0, 80.0,
        "ENTRY:MOMENTUM", 80, tp1=102.0, tp2=104.0,
        rr1=1.0, rr2=2.0, entry_type="MOMENTUM", regime="TRENDING_UP",
    )
    pos = Position(signal, 0.25)
    pos.qty_original = 0.5
    pos.qty = 0.25
    pos.tp1_hit = True
    pos.sl = pos.entry
    pos.trailing_sl = pos.entry
    pos.trailing_active = True
    pos.trailing_milestone = 2
    pos.peak_pnl = 1.5
    return pos


class DurableExecutionRestartTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = {}
        self.original_save = db.save_key_value
        self.original_load = db.load_key_value
        self.original_pg_check = db.configured_postgres_unavailable

        async def save(key, value, *, strict=False):
            self.store[key] = value
            return True

        async def load(key, *, strict=False):
            return self.store.get(key)

        db.save_key_value = save
        db.load_key_value = load
        db.configured_postgres_unavailable = lambda: False

    async def asyncTearDown(self):
        db.save_key_value = self.original_save
        db.load_key_value = self.original_load
        db.configured_postgres_unavailable = self.original_pg_check

    async def test_paper_position_lifecycle_survives_restart(self):
        first = _engine()
        first.positions["BTCUSDT"] = _position()
        first._trade_ids["BTCUSDT"] = 77
        first._cooldown["ETHUSDT"] = time.time() + 600
        first._paper_balance = 14.50
        first.risk.peak_balance = 20.0
        first._durable_order_lock = __import__("asyncio").Lock()
        first._durable_paper_lock = __import__("asyncio").Lock()
        first._durable_state_errors = set()
        first._durable_state_ok = True
        first._paper_last_snapshot = None

        self.assertTrue(await durable.persist_paper_runtime(first, "before_restart"))

        restarted = _engine()
        self.assertTrue(await durable.restore_engine_state(restarted))
        restored = restarted.positions["BTCUSDT"]
        self.assertEqual(restarted._trade_ids["BTCUSDT"], 77)
        self.assertAlmostEqual(restarted.risk.balance, 14.50)
        self.assertAlmostEqual(restarted.risk.peak_balance, 20.0)
        self.assertTrue(restored.tp1_hit)
        self.assertTrue(restored.trailing_active)
        self.assertEqual(restored.trailing_milestone, 2)
        self.assertAlmostEqual(restored.qty, 0.25)
        self.assertAlmostEqual(restored.qty_original, 0.5)
        self.assertAlmostEqual(restored.sl, 100.0)

    async def test_invalid_position_snapshot_fails_closed(self):
        self.store[durable.PAPER_STATE_KEY] = json.dumps({
            "version": 1,
            "balance": 20,
            "peak_balance": 20,
            "positions": [{"symbol": "BTCUSDT", "direction": "LONG"}],
            "trade_ids": {},
            "cooldown": {},
        })
        restarted = _engine()
        self.assertFalse(await durable.restore_engine_state(restarted))
        self.assertFalse(durable.can_open(restarted))
        self.assertEqual(restarted.positions, {})

    async def test_order_registry_roundtrip_preserves_exchange_ids(self):
        first = _engine()
        order, _ = first.orders.get_or_create(
            "bgx7-abc", "BTCUSDT", "Buy", 0.25
        )
        order.transition(OrderState.SUBMITTING, source="REST")
        order.transition(OrderState.SUBMITTED, order_id="exchange-7", source="REST")
        first.orders.index_order_id("exchange-7", order.client_oid)
        first._durable_order_lock = __import__("asyncio").Lock()
        first._durable_paper_lock = __import__("asyncio").Lock()
        first._durable_state_errors = set()
        first._durable_state_ok = True
        first._paper_last_snapshot = None

        self.assertTrue(await durable.persist_orders(first, "submitted"))
        restarted = _engine()
        self.assertTrue(await durable.restore_engine_state(restarted))
        restored = restarted.orders.get_by_order_id("exchange-7")
        self.assertIsNotNone(restored)
        self.assertEqual(restored.client_oid, "bgx7-abc")
        self.assertEqual(restored.state, OrderState.SUBMITTED)

    async def test_unresolved_live_intent_blocks_without_resubmission(self):
        first = _engine(paper=False)
        order, _ = first.orders.get_or_create(
            "bgx7-unresolved", "ETHUSDT", "Sell", 1.0
        )
        order.transition(OrderState.SUBMITTING, source="REST")
        first._durable_order_lock = __import__("asyncio").Lock()
        first._durable_paper_lock = __import__("asyncio").Lock()
        first._durable_state_errors = set()
        first._durable_state_ok = True
        first._paper_last_snapshot = None
        await durable.persist_orders(first, "before_dispatch")

        restarted = _engine(paper=False)
        restarted.client = SimpleNamespace(
            get_order_by_client_oid=AsyncMock(return_value={}),
            place_order=AsyncMock(),
        )
        await durable.restore_engine_state(restarted)
        self.assertFalse(await durable.reconcile_orders(restarted))
        self.assertFalse(durable.can_open(restarted))
        restarted.client.place_order.assert_not_awaited()

    async def test_paper_pending_intent_is_failed_not_resent(self):
        process = _engine(paper=True)
        order, _ = process.orders.get_or_create(
            "bgx7-paper-pending", "SOLUSDT", "Buy", 2.0
        )
        order.transition(OrderState.SUBMITTING, source="REST")
        process._durable_order_lock = __import__("asyncio").Lock()
        process._durable_paper_lock = __import__("asyncio").Lock()
        process._durable_state_errors = set()
        process._durable_state_ok = True
        process._paper_last_snapshot = None
        await durable.persist_orders(process, "before_dispatch")

        restarted = _engine(paper=True)
        await durable.restore_engine_state(restarted)
        self.assertTrue(await durable.reconcile_orders(restarted))
        self.assertEqual(
            restarted.orders.get("bgx7-paper-pending").state, OrderState.FAILED
        )
        self.assertTrue(durable.can_open(restarted))

    async def test_paper_submitted_intent_is_cancelled_not_resent(self):
        process = _engine(paper=True)
        order, _ = process.orders.get_or_create(
            "bgx7-paper-submitted", "SOLUSDT", "Buy", 2.0
        )
        order.transition(OrderState.SUBMITTING, source="REST")
        order.transition(
            OrderState.SUBMITTED, order_id="paper-crash", source="REST"
        )
        process.orders.index_order_id("paper-crash", order.client_oid)
        process._durable_order_lock = __import__("asyncio").Lock()
        process._durable_paper_lock = __import__("asyncio").Lock()
        process._durable_state_errors = set()
        process._durable_state_ok = True
        process._paper_last_snapshot = None
        await durable.persist_orders(process, "submitted")

        restarted = _engine(paper=True)
        await durable.restore_engine_state(restarted)
        self.assertTrue(await durable.reconcile_orders(restarted))
        self.assertEqual(
            restarted.orders.get("bgx7-paper-submitted").state,
            OrderState.CANCELLED,
        )
        self.assertTrue(durable.can_open(restarted))


if __name__ == "__main__":
    unittest.main()
