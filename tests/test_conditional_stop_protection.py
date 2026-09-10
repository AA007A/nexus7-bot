import unittest
from unittest.mock import AsyncMock

from bot.conditional_stop_protection import conditional_stop_confirmed


class _Client:
    def __init__(self, orders):
        self.get_stop_orders = AsyncMock(return_value=orders)


class ConditionalStopProtectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_long_full_reduce_only_stop_is_protected(self):
        position = {
            "symbol": "XRPUSDT", "size": "10", "side": "Buy",
            "entryPrice": "2.0", "markPrice": "1.9", "stopLoss": "0",
        }
        orders = [{
            "symbol": "XRPUSDTM", "side": "sell", "stopPrice": "1.7",
            "reduceOnly": True, "closeOrder": False, "size": "10",
            "isActive": True, "stopTriggered": False,
        }]
        protected, evidence = await conditional_stop_confirmed(_Client(orders), position)
        self.assertTrue(protected)
        self.assertEqual(evidence, "conditional_reduce_only")

    async def test_short_close_order_stop_is_protected(self):
        position = {
            "symbol": "ETHUSDT", "size": "5", "side": "Sell",
            "entryPrice": "100", "markPrice": "95", "stopLoss": "0",
        }
        orders = [{
            "symbol": "ETHUSDTM", "side": "buy", "stopPrice": "105",
            "reduceOnly": False, "closeOrder": True,
            "isActive": True, "stopTriggered": False,
        }]
        protected, evidence = await conditional_stop_confirmed(_Client(orders), position)
        self.assertTrue(protected)
        self.assertEqual(evidence, "conditional_close_order")

    async def test_partial_stop_does_not_claim_full_protection(self):
        position = {
            "symbol": "XRPUSDT", "size": "10", "side": "Buy",
            "entryPrice": "2.0", "markPrice": "1.9", "stopLoss": "0",
        }
        orders = [{
            "symbol": "XRPUSDTM", "side": "sell", "stopPrice": "1.7",
            "reduceOnly": True, "size": "4", "isActive": True,
            "stopTriggered": False,
        }]
        protected, _ = await conditional_stop_confirmed(_Client(orders), position)
        self.assertFalse(protected)

    async def test_non_reduce_only_stop_entry_is_rejected(self):
        position = {
            "symbol": "XRPUSDT", "size": "10", "side": "Buy",
            "entryPrice": "2.0", "markPrice": "1.9", "stopLoss": "0",
        }
        orders = [{
            "symbol": "XRPUSDTM", "side": "sell", "stopPrice": "1.7",
            "reduceOnly": False, "closeOrder": False, "size": "10",
            "isActive": True, "stopTriggered": False,
        }]
        protected, _ = await conditional_stop_confirmed(_Client(orders), position)
        self.assertFalse(protected)

    async def test_wrong_side_or_triggered_stop_is_rejected(self):
        position = {
            "symbol": "XRPUSDT", "size": "10", "side": "Buy",
            "entryPrice": "2.0", "markPrice": "1.9", "stopLoss": "0",
        }
        orders = [
            {"symbol": "XRPUSDTM", "side": "buy", "stopPrice": "1.7",
             "reduceOnly": True, "size": "10", "isActive": True},
            {"symbol": "XRPUSDTM", "side": "sell", "stopPrice": "1.7",
             "reduceOnly": True, "size": "10", "isActive": False,
             "stopTriggered": True},
        ]
        protected, _ = await conditional_stop_confirmed(_Client(orders), position)
        self.assertFalse(protected)

    async def test_stop_on_wrong_price_side_is_rejected(self):
        position = {
            "symbol": "XRPUSDT", "size": "10", "side": "Buy",
            "entryPrice": "2.0", "markPrice": "1.9", "stopLoss": "0",
        }
        orders = [{
            "symbol": "XRPUSDTM", "side": "sell", "stopPrice": "2.1",
            "reduceOnly": True, "size": "10", "isActive": True,
        }]
        protected, _ = await conditional_stop_confirmed(_Client(orders), position)
        self.assertFalse(protected)

    async def test_read_failure_fails_closed(self):
        position = {
            "symbol": "XRPUSDT", "size": "10", "side": "Buy",
            "entryPrice": "2.0", "markPrice": "1.9", "stopLoss": "0",
        }
        client = _Client([])
        client.get_stop_orders.side_effect = RuntimeError("read failed")
        protected, evidence = await conditional_stop_confirmed(client, position)
        self.assertFalse(protected)
        self.assertEqual(evidence, "stop_orders_unconfirmed")

    async def test_inline_stop_skips_conditional_read(self):
        position = {
            "symbol": "XRPUSDT", "size": "10", "side": "Buy",
            "entryPrice": "2.0", "markPrice": "1.9", "stopLoss": "1.7",
        }
        client = _Client([])
        protected, evidence = await conditional_stop_confirmed(client, position)
        self.assertTrue(protected)
        self.assertEqual(evidence, "inline_stop")
        client.get_stop_orders.assert_not_called()


if __name__ == "__main__":
    unittest.main()
