"""A rejected order must not be retried with a different leverage."""
import unittest
from unittest.mock import AsyncMock, patch

from bot.config import cfg
from bot.kucoin import KuCoinClient


class LeverageNoFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejection_has_one_dispatch_at_configured_leverage(self):
        client = KuCoinClient()
        client._instruments = {
            "TESTUSDT": {
                "multiplier": 0.001,
                "lotSize": 1,
                "minQty": 1,
                "tickSize": 0.01,
            }
        }
        client._post = AsyncMock(return_value={})
        client._position_exists = AsyncMock(return_value=False)

        with patch("bot.kucoin.PAPER_TRADE", False), \
             patch("bot.kucoin.API_KEY", "test-key"):
            result = await client.place_order("TESTUSDT", "Buy", 0.001)

        client._post.assert_awaited_once()
        endpoint, body = client._post.call_args.args
        self.assertEqual(endpoint, "/api/v1/orders")
        self.assertEqual(body["leverage"], str(cfg.LEVERAGE))
        client._position_exists.assert_not_awaited()
        self.assertFalse(result.get("orderId"))


if __name__ == "__main__":
    unittest.main()
