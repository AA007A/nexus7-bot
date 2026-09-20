import unittest
from unittest.mock import AsyncMock, patch

from bot import kucoin


class LiveAdapterChaosTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = kucoin.KuCoinClient()
        self.client._instruments = {
            "BTCUSDT": {
                "tickSize": 0.1,
                "lotSize": 1,
                "qtyStep": 1,
                "multiplier": 0.001,
                "minQty": 1,
                "minBaseQty": 0.001,
                "minNotional": 0,
                "kucoinSymbol": "XBTUSDTM",
            }
        }

    async def test_ambiguous_submission_is_not_blindly_resubmitted(self):
        """A lost response must resolve by clientOid, not create a new order."""
        with patch.object(kucoin, "PAPER_TRADE", False), \
             patch.object(kucoin, "API_KEY", "test-key"), \
             patch.object(self.client, "_post", AsyncMock(return_value={
                 "clientOid": "bgx7-chaos",
                 "_ambiguous": True,
             })) as post, \
             patch.object(self.client, "_recover_ambiguous_order", AsyncMock(
                 return_value={"orderId": "kc-order-1", "clientOid": "bgx7-chaos"}
             )) as recover, \
             patch.object(self.client, "set_position_stops", AsyncMock(return_value=True)), \
             patch("bot.kucoin.asyncio.sleep", AsyncMock()):
            out = await self.client.place_order(
                "BTCUSDT", "Buy", 0.001, sl=99000, tp=103000,
                idem_key="chaos-idem", single_submission=True,
            )

        self.assertEqual(out.get("orderId"), "kc-order-1")
        self.assertEqual(post.await_count, 1, "ambiguous LIVE submission must not be blindly retried")
        recover.assert_awaited_once()

    async def test_open_order_with_failed_protection_is_explicitly_flagged(self):
        """If entry exists but SL/TP cannot be confirmed, caller must see failure."""
        with patch.object(kucoin, "PAPER_TRADE", False), \
             patch.object(kucoin, "API_KEY", "test-key"), \
             patch.object(self.client, "_post", AsyncMock(return_value={"orderId": "kc-order-2"})), \
             patch.object(self.client, "set_position_stops", AsyncMock(return_value=False)) as stops, \
             patch("bot.kucoin.asyncio.sleep", AsyncMock()):
            out = await self.client.place_order(
                "BTCUSDT", "Sell", 0.001, sl=103000, tp=97000,
                idem_key="chaos-protection", single_submission=True,
            )

        self.assertEqual(out.get("orderId"), "kc-order-2")
        self.assertTrue(out.get("sl_tp_failed"))
        stops.assert_awaited_once()

    async def test_position_probe_failure_fails_safe_against_duplicate_entry(self):
        """Unknown exchange exposure must be treated as existing exposure."""
        with patch.object(self.client, "get_positions", AsyncMock(side_effect=TimeoutError("network"))):
            exists = await self.client._position_exists("BTCUSDT")
        self.assertTrue(exists)

    async def test_db_failure_immediately_before_entry_post_blocks_exchange_mutation(self):
        from types import SimpleNamespace
        self.client._engine = SimpleNamespace()
        with patch.object(kucoin, "PAPER_TRADE", False), \
             patch.object(kucoin, "API_KEY", "test-key"), \
             patch("bot.critical_state.critical_state.assert_available_for_new_risk", side_effect=RuntimeError("db")), \
             patch.object(self.client, "_post", AsyncMock()) as post:
            with self.assertRaises(RuntimeError):
                await self.client.place_order("BTCUSDT","Buy",0.001,idem_key="db-fail")
        post.assert_not_awaited()

    async def test_reduce_only_remains_available_when_critical_db_is_down(self):
        with patch.object(kucoin, "PAPER_TRADE", False), \
             patch.object(kucoin, "API_KEY", "test-key"), \
             patch("bot.critical_state.critical_state.assert_available_for_new_risk", side_effect=RuntimeError("db")), \
             patch.object(self.client, "_post", AsyncMock(return_value={"orderId":"reduce-1"})) as post:
            out=await self.client.place_order("BTCUSDT","Sell",0.001,reduce_only=True)
        self.assertEqual(out.get("orderId"),"reduce-1")
        post.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
