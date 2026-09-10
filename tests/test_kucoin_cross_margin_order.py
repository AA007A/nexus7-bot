import unittest
from types import SimpleNamespace

from bot import kucoin_cross_margin_order


class _Client:
    async def _post(self, endpoint, body, *args, **kwargs):
        return {"endpoint": endpoint, "body": body, "kwargs": kwargs}


class KuCoinCrossMarginOrderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        if hasattr(_Client, "_cross_margin_order_mode_patched"):
            delattr(_Client, "_cross_margin_order_mode_patched")
        kucoin_cross_margin_order.install(
            _Client,
            SimpleNamespace(warning=lambda *args, **kwargs: None),
        )

    async def test_order_payload_forces_explicit_cross_margin(self):
        client = _Client()
        original = {"symbol": "XBTUSDTM", "side": "buy", "marginMode": "ISOLATED"}
        result = await client._post("/api/v1/orders", original, single_attempt=True)

        self.assertEqual(result["body"]["marginMode"], "CROSS")
        self.assertEqual(original["marginMode"], "ISOLATED")
        self.assertTrue(result["kwargs"]["single_attempt"])

    async def test_non_order_post_is_unchanged(self):
        client = _Client()
        original = {"symbol": "XBTUSDTM"}
        result = await client._post("/api/v1/position/trading-stop", original)

        self.assertEqual(result["body"], original)
        self.assertNotIn("marginMode", result["body"])


if __name__ == "__main__":
    unittest.main()
