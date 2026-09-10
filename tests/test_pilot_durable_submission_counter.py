"""Regression tests for the durable two-order pilot submission budget."""
import os
import unittest
from unittest.mock import AsyncMock, Mock, patch

from bot import pilot_submission_counter as counter


class PilotDurableSubmissionCounterTests(unittest.IsolatedAsyncioTestCase):
    def _client_class(self):
        class Client:
            def __init__(self):
                self.original_calls = 0

            async def place_order(self, *args, **kwargs):
                self.original_calls += 1
                return {"orderId": "offline"}

        counter.install(Client, Mock())
        return Client

    async def test_reservation_is_confirmed_before_new_entry_dispatch(self):
        Client = self._client_class()
        client = Client()
        with patch.object(
            counter, "reserve_submission", new=AsyncMock(return_value=(True, 1))
        ) as reserve:
            result = await client.place_order(
                symbol="BTCUSDT", side="Buy", qty=1,
                idem_key="idem-1", single_submission=True,
            )
        self.assertEqual(result["orderId"], "offline")
        self.assertEqual(client.original_calls, 1)
        reserve.assert_awaited_once()

    async def test_exhausted_durable_budget_blocks_exchange_dispatch(self):
        Client = self._client_class()
        client = Client()
        with patch.object(
            counter, "reserve_submission", new=AsyncMock(return_value=(False, 2))
        ):
            result = await client.place_order(
                symbol="BTCUSDT", side="Buy", qty=1,
                idem_key="idem-3", single_submission=True,
            )
        self.assertEqual(result, {})
        self.assertEqual(client.original_calls, 0)

    async def test_persistence_error_fails_closed_before_dispatch(self):
        Client = self._client_class()
        client = Client()
        with patch.object(
            counter, "reserve_submission", new=AsyncMock(side_effect=RuntimeError("db"))
        ):
            result = await client.place_order(
                symbol="BTCUSDT", side="Buy", qty=1,
                idem_key="idem-1", single_submission=True,
            )
        self.assertEqual(result, {})
        self.assertEqual(client.original_calls, 0)

    async def test_reduce_only_exit_does_not_consume_new_entry_budget(self):
        Client = self._client_class()
        client = Client()
        reserve = AsyncMock(side_effect=AssertionError("must not reserve reduceOnly"))
        with patch.object(counter, "reserve_submission", new=reserve):
            result = await client.place_order(
                symbol="BTCUSDT", side="Sell", qty=1,
                reduce_only=True, single_submission=True,
            )
        self.assertEqual(result["orderId"], "offline")
        self.assertEqual(client.original_calls, 1)
        reserve.assert_not_awaited()

    async def test_non_pilot_submission_is_unchanged(self):
        Client = self._client_class()
        client = Client()
        reserve = AsyncMock(side_effect=AssertionError("non-pilot must bypass"))
        with patch.object(counter, "reserve_submission", new=reserve):
            result = await client.place_order(
                symbol="BTCUSDT", side="Buy", qty=1,
                single_submission=False,
            )
        self.assertEqual(result["orderId"], "offline")
        reserve.assert_not_awaited()

    def test_order_token_is_idempotent_for_same_logical_order(self):
        a = counter._order_token("BTCUSDT", "Buy", 1, "idem-1")
        b = counter._order_token("BTCUSDT", "Buy", 1, "idem-1")
        c = counter._order_token("BTCUSDT", "Buy", 1, "idem-2")
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)

    def test_session_key_survives_restart_identity_and_changes_on_deploy(self):
        with patch.dict(os.environ, {"RAILWAY_DEPLOYMENT_ID": "deploy-a"}, clear=False):
            os.environ.pop("PILOT_SESSION_ID", None)
            first = counter._state_key()
            second = counter._state_key()
        with patch.dict(os.environ, {"RAILWAY_DEPLOYMENT_ID": "deploy-b"}, clear=False):
            os.environ.pop("PILOT_SESSION_ID", None)
            third = counter._state_key()
        self.assertEqual(first, second)
        self.assertNotEqual(first, third)

    def test_malformed_state_is_fail_closed(self):
        with self.assertRaises(RuntimeError):
            counter._decode('{"version":1,"order_tokens":"not-a-list"}')


if __name__ == "__main__":
    unittest.main()
