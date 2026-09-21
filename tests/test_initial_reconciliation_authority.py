import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.initial_reconciliation import reconcile_initial_state
from bot.order_state import OrderRegistry


def _engine(*, positions=None, orders=None):
    client = SimpleNamespace(
        get_positions=AsyncMock(return_value=[] if positions is None else positions),
        _get=AsyncMock(return_value={"items": [] if orders is None else orders}),
    )
    return SimpleNamespace(
        client=client,
        connected=True,
        paper_trade=False,
        orders=OrderRegistry(),
        _durable_state_ok=True,
        _recovered_position_symbols=set(),
        _external_position_symbols=set(),
        _initial_reconciliation_complete=True,
    )


class InitialReconciliationAuthorityTests(unittest.IsolatedAsyncioTestCase):
    async def test_zero_exposure_completes_from_fresh_exchange_truth(self):
        engine = _engine()
        completed = await reconcile_initial_state(
            engine, durable_orders_reconciled=True
        )
        self.assertTrue(completed)
        self.assertTrue(engine._initial_reconciliation_complete)
        self.assertEqual(engine._initial_reconciliation_receipt["positions"], 0)
        self.assertEqual(engine._initial_reconciliation_receipt["active_orders"], 0)
        engine.client.get_positions.assert_awaited_once()
        engine.client._get.assert_awaited_once_with(
            "/api/v1/orders", {"status": "active"}, auth=True
        )

    async def test_durable_ready_is_not_reconciliation_complete(self):
        engine = _engine()
        completed = await reconcile_initial_state(
            engine, durable_orders_reconciled=False
        )
        self.assertTrue(engine._durable_state_ok)
        self.assertFalse(completed)
        self.assertFalse(engine._initial_reconciliation_complete)
        engine.client.get_positions.assert_not_awaited()

    async def test_unclassified_live_position_fails_closed(self):
        engine = _engine(positions=[{"symbol": "BTCUSDT", "size": 0.01}])
        with patch(
            "bot.initial_reconciliation.conditional_stop_confirmed",
            AsyncMock(return_value=(True, "inline_stop")),
        ):
            completed = await reconcile_initial_state(
                engine, durable_orders_reconciled=True
            )
        self.assertFalse(completed)
        self.assertFalse(engine._initial_reconciliation_complete)

    async def test_classified_position_requires_protection_readback_evidence(self):
        position = {"symbol": "BTCUSDT", "size": 0.01}
        engine = _engine(positions=[position])
        engine._external_position_symbols = {"BTCUSDT"}
        verifier = AsyncMock(return_value=(False, "no_full_protective_stop"))
        with patch(
            "bot.initial_reconciliation.conditional_stop_confirmed", verifier
        ):
            completed = await reconcile_initial_state(
                engine, durable_orders_reconciled=True
            )
        self.assertTrue(completed)
        verifier.assert_awaited_once_with(engine.client, position)
        self.assertEqual(
            engine._initial_reconciliation_receipt["unprotected_positions"], 1
        )

    async def test_malformed_exchange_truth_fails_closed(self):
        engine = _engine()
        engine.client.get_positions.return_value = {"items": []}
        completed = await reconcile_initial_state(
            engine, durable_orders_reconciled=True
        )
        self.assertFalse(completed)
        self.assertFalse(engine._initial_reconciliation_complete)


if __name__ == "__main__":
    unittest.main()
