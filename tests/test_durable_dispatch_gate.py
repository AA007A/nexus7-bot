"""The durable intent must commit before any exchange-facing dispatch."""
import asyncio
from unittest.mock import AsyncMock, patch

from bot import database as db
from bot import durable_execution as durable
from bot.order_state import OrderState
from tests.test_ai_gate import EngineFixture, approval


class DurableDispatchGateTests(EngineFixture):
    async def test_database_failure_blocks_before_place_order(self):
        self.engine._durable_state_enforced = True
        self.engine._durable_state_ok = True
        self.engine._durable_state_errors = set()
        self.engine._durable_order_lock = asyncio.Lock()
        self.engine._durable_paper_lock = asyncio.Lock()

        with patch(
            "bot.database.save_key_value",
            AsyncMock(side_effect=db.PersistenceError("offline failure")),
        ):
            await self.attempt(approval())

        self.client.place_order.assert_not_awaited()
        self.assertFalse(durable.can_open(self.engine))
        pending = self.engine.orders.pending_orders()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].state, OrderState.SUBMITTING)
        self.assertTrue(pending[0].client_oid.startswith("bgx7-"))


if __name__ == "__main__":
    import unittest
    unittest.main()
