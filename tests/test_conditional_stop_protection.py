import unittest
from unittest.mock import AsyncMock

from bot.conditional_stop_protection import conditional_stop_confirmed


class _Client:
    def __init__(self, orders, instruments=None):
        self.get_stop_orders = AsyncMock(return_value=orders)
        self._instruments = instruments if instruments is not None else {
            "XRPUSDT": {"multiplier": "1", "lotSize": "1", "minQty": "1"},
            "ETHUSDT": {"multiplier": "1", "lotSize": "1", "minQty": "1"},
        }

    def get_instruments(self):
        return self._instruments


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

    async def test_eth_partial_contract_stop_cannot_cover_normalized_base_position(self):
        instruments = {
            "ETHUSDT": {"multiplier": "0.01", "lotSize": "1", "minQty": "1"},
        }
        position = {
            "symbol": "ETHUSDT", "size": "0.21", "sizeUnit": "BASE_ASSET",
            "sizeContracts": "21", "side": "Buy", "entryPrice": "2530",
            "markPrice": "2525", "stopLoss": "0",
        }
        orders = [{
            "symbol": "ETHUSDTM", "side": "sell", "stopPrice": "2500",
            "reduceOnly": True, "size": "1", "isActive": True,
            "stopTriggered": False,
        }]
        protected, evidence = await conditional_stop_confirmed(
            _Client(orders, instruments), position
        )
        self.assertFalse(protected)
        self.assertEqual(evidence, "no_full_protective_stop")

    async def test_eth_full_21_contract_stop_covers_point_21_base_position(self):
        instruments = {
            "ETHUSDT": {"multiplier": "0.01", "lotSize": "1", "minQty": "1"},
        }
        position = {
            "symbol": "ETHUSDT", "size": "0.21", "sizeUnit": "BASE_ASSET",
            "sizeContracts": "21", "side": "Buy", "entryPrice": "2530",
            "markPrice": "2525", "stopLoss": "0",
        }
        orders = [{
            "symbol": "ETHUSDTM", "side": "sell", "stopPrice": "2500",
            "reduceOnly": True, "size": "21", "isActive": True,
            "stopTriggered": False,
        }]
        protected, evidence = await conditional_stop_confirmed(
            _Client(orders, instruments), position
        )
        self.assertTrue(protected)
        self.assertEqual(evidence, "conditional_reduce_only")

    async def test_missing_instrument_metadata_fails_closed_for_native_contract_stop(self):
        position = {
            "symbol": "ETHUSDT", "size": "0.21", "sizeUnit": "BASE_ASSET",
            "side": "Buy", "entryPrice": "2530", "markPrice": "2525",
            "stopLoss": "0",
        }
        orders = [{
            "symbol": "ETHUSDTM", "side": "sell", "stopPrice": "2500",
            "reduceOnly": True, "size": "21", "isActive": True,
        }]
        protected, evidence = await conditional_stop_confirmed(_Client(orders, {}), position)
        self.assertFalse(protected)
        self.assertEqual(evidence, "no_full_protective_stop")

    async def test_explicit_base_unit_stop_does_not_convert_twice(self):
        position = {
            "symbol": "ETHUSDT", "size": "0.21", "sizeUnit": "BASE_ASSET",
            "side": "Buy", "entryPrice": "2530", "markPrice": "2525",
            "stopLoss": "0",
        }
        orders = [{
            "symbol": "ETHUSDTM", "side": "sell", "stopPrice": "2500",
            "reduceOnly": True, "size": "0.21", "sizeUnit": "BASE_ASSET",
            "isActive": True,
        }]
        protected, evidence = await conditional_stop_confirmed(_Client(orders, {}), position)
        self.assertTrue(protected)
        self.assertEqual(evidence, "conditional_reduce_only")

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
