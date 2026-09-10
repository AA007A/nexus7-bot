import unittest
from unittest.mock import AsyncMock, patch

from bot import database as db
from bot.capital_flow_reconciliation import (
    LAST_FLOW_OFFSET_KEY,
    reconcile_external_capital_flows,
)


class DummyClient:
    def __init__(self, payload):
        self._get = AsyncMock(return_value=payload)


class CapitalFlowReconciliationTests(unittest.IsolatedAsyncioTestCase):
    async def test_bootstrap_matching_latest_transferout_rebases_once_and_checkpoints(self):
        client = DummyClient({
            "dataList": [
                {
                    "time": 1000,
                    "type": "RealisedPNL",
                    "amount": -1.5,
                    "fee": 0,
                    "accountEquity": 34.8664,
                    "status": "Completed",
                    "offset": 10,
                    "currency": "USDT",
                },
                {
                    "time": 2000,
                    "type": "TransferOut",
                    "amount": -14.0,
                    "fee": 0,
                    "accountEquity": 20.8664,
                    "status": "Completed",
                    "offset": 11,
                    "currency": "USDT",
                },
            ]
        })
        risk = object()

        with patch("bot.capital_flow_reconciliation.db.load_key_value", AsyncMock(return_value=None)), \
             patch("bot.capital_flow_reconciliation.db.save_key_value", AsyncMock(return_value=True)) as save, \
             patch("bot.capital_flow_reconciliation.rebase_real_account_peak_for_external_flow", AsyncMock(return_value=22.9)) as rebase:
            result = await reconcile_external_capital_flows(client, risk, 20.8664, strict=True)

        self.assertEqual(result["applied"], 1)
        self.assertTrue(result["bootstrap"])
        rebase.assert_awaited_once_with(
            risk,
            20.8664,
            pre_flow_equity=34.8664,
            post_flow_equity=20.8664,
            flow_type="TransferOut",
            flow_amount=14.0,
            flow_offset="11",
            strict=True,
        )
        save.assert_awaited_once_with(LAST_FLOW_OFFSET_KEY, "11", strict=True)

    async def test_bootstrap_can_match_recent_transfer_even_if_not_newest_transfer(self):
        client = DummyClient({
            "dataList": [
                {
                    "time": 2000,
                    "type": "TransferOut",
                    "amount": -14.0,
                    "accountEquity": 20.8664,
                    "status": "Completed",
                    "offset": 11,
                },
                {
                    "time": 3000,
                    "type": "TransferIn",
                    "amount": 5.0,
                    "accountEquity": 84.9133,
                    "status": "Completed",
                    "offset": 12,
                },
            ]
        })
        risk = object()

        with patch("bot.capital_flow_reconciliation.db.load_key_value", AsyncMock(return_value=None)), \
             patch("bot.capital_flow_reconciliation.db.save_key_value", AsyncMock(return_value=True)) as save, \
             patch("bot.capital_flow_reconciliation.rebase_real_account_peak_for_external_flow", AsyncMock(return_value=22.9)) as rebase:
            result = await reconcile_external_capital_flows(client, risk, 20.8664, strict=True)

        self.assertEqual(result["applied"], 1)
        kwargs = rebase.await_args.kwargs
        self.assertEqual(kwargs["flow_offset"], "11")
        self.assertEqual(kwargs["flow_amount"], 14.0)
        save.assert_awaited_once_with(LAST_FLOW_OFFSET_KEY, "12", strict=True)

    async def test_bootstrap_equity_mismatch_does_not_rebase_but_checkpoints(self):
        client = DummyClient({
            "dataList": [{
                "time": 2000,
                "type": "TransferOut",
                "amount": -14.0,
                "fee": 0,
                "accountEquity": 20.8664,
                "status": "Completed",
                "offset": 11,
                "currency": "USDT",
            }]
        })
        risk = object()

        with patch("bot.capital_flow_reconciliation.db.load_key_value", AsyncMock(return_value=None)), \
             patch("bot.capital_flow_reconciliation.db.save_key_value", AsyncMock(return_value=True)) as save, \
             patch("bot.capital_flow_reconciliation.rebase_real_account_peak_for_external_flow", AsyncMock()) as rebase:
            result = await reconcile_external_capital_flows(client, risk, 18.0, strict=True)

        self.assertEqual(result["applied"], 0)
        rebase.assert_not_awaited()
        save.assert_awaited_once_with(LAST_FLOW_OFFSET_KEY, "11", strict=True)

    async def test_existing_cursor_processes_only_new_completed_transfers(self):
        client = DummyClient({
            "dataList": [
                {
                    "time": 1000,
                    "type": "TransferOut",
                    "amount": -2.0,
                    "accountEquity": 98.0,
                    "status": "Completed",
                    "offset": 10,
                },
                {
                    "time": 2000,
                    "type": "TransferIn",
                    "amount": 10.0,
                    "accountEquity": 108.0,
                    "status": "Completed",
                    "offset": 11,
                },
                {
                    "time": 3000,
                    "type": "TransferOut",
                    "amount": -3.0,
                    "accountEquity": 105.0,
                    "status": "Pending",
                    "offset": 12,
                },
                {
                    "time": 4000,
                    "type": "RealisedPNL",
                    "amount": 4.0,
                    "accountEquity": 112.0,
                    "status": "Completed",
                    "offset": 13,
                },
            ]
        })
        risk = object()

        with patch("bot.capital_flow_reconciliation.db.load_key_value", AsyncMock(return_value="10")), \
             patch("bot.capital_flow_reconciliation.db.save_key_value", AsyncMock(return_value=True)) as save, \
             patch("bot.capital_flow_reconciliation.rebase_real_account_peak_for_external_flow", AsyncMock(return_value=120.0)) as rebase:
            result = await reconcile_external_capital_flows(client, risk, 108.0, strict=True)

        self.assertEqual(result["applied"], 1)
        rebase.assert_awaited_once()
        kwargs = rebase.await_args.kwargs
        self.assertEqual(kwargs["flow_type"], "TransferIn")
        self.assertEqual(kwargs["flow_amount"], 10.0)
        self.assertEqual(kwargs["pre_flow_equity"], 98.0)
        self.assertEqual(kwargs["post_flow_equity"], 108.0)
        save.assert_awaited_once_with(LAST_FLOW_OFFSET_KEY, "11", strict=True)

    async def test_no_completed_transfers_does_nothing(self):
        client = DummyClient({
            "dataList": [
                {
                    "time": 1000,
                    "type": "RealisedPNL",
                    "amount": 2.0,
                    "accountEquity": 102.0,
                    "status": "Completed",
                    "offset": 1,
                }
            ]
        })
        with patch("bot.capital_flow_reconciliation.db.load_key_value", AsyncMock()) as load:
            result = await reconcile_external_capital_flows(client, object(), 102.0, strict=True)
        self.assertEqual(result, {"applied": 0, "bootstrap": False})
        load.assert_not_awaited()

    async def test_malformed_cursor_fails_closed(self):
        client = DummyClient({
            "dataList": [{
                "time": 2000,
                "type": "TransferOut",
                "amount": -5.0,
                "accountEquity": 95.0,
                "status": "Completed",
                "offset": 11,
            }]
        })
        with patch("bot.capital_flow_reconciliation.db.load_key_value", AsyncMock(return_value="bad-offset")):
            with self.assertRaises(db.PersistenceError):
                await reconcile_external_capital_flows(client, object(), 95.0, strict=True)

    async def test_ledger_failure_fails_closed(self):
        client = DummyClient(None)
        with self.assertRaises(RuntimeError):
            await reconcile_external_capital_flows(client, object(), 95.0, strict=True)


if __name__ == "__main__":
    unittest.main()
