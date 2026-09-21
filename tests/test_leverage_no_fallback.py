"""A rejected order must not be retried with a different leverage."""
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
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
        client._engine = SimpleNamespace()

        with (
            patch("bot.kucoin.PAPER_TRADE", False),
            patch("bot.kucoin.API_KEY", "test-key"),
            patch("bot.critical_state.critical_state.assert_available_for_new_risk"),
            patch("bot.execution_ownership.acquire_execution_ownership", AsyncMock(return_value=SimpleNamespace(expires_at=datetime.now(timezone.utc)+timedelta(seconds=30)))),
            patch("bot.execution_ownership.validate_execution_ownership", AsyncMock(return_value=datetime.now(timezone.utc)+timedelta(seconds=30))),
            patch("bot.runtime_readiness.assert_ready_for_new_entries"),
        ):
            result = await client.place_order("TESTUSDT", "Buy", 0.001)

        client._post.assert_awaited_once()
        endpoint, body = client._post.call_args.args
        self.assertEqual(endpoint, "/api/v1/orders")
        self.assertEqual(body["leverage"], str(cfg.LEVERAGE))
        client._position_exists.assert_not_awaited()
        self.assertFalse(result.get("orderId"))


if __name__ == "__main__":
    unittest.main()
