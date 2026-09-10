import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot import kucoin_native_tpsl as hardening


class _Log:
    warning = staticmethod(lambda *a, **k: None)
    info = staticmethod(lambda *a, **k: None)
    error = staticmethod(lambda *a, **k: None)


class _FakeClient:
    def __init__(self):
        self._post = AsyncMock(return_value={"orderId": "kc-1"})
        self.get_order_by_client_oid = AsyncMock(return_value={})
        self.original_calls = []

    async def place_order(self, symbol, side, qty, sl=0, tp=0, instruments=None,
                          reduce_only=False, idem_key=None, single_submission=False):
        self.original_calls.append((symbol, side, qty, reduce_only))
        return {"orderId": "original"}

    def _round_qty(self, qty, symbol):
        return 2

    def build_client_oid(self, symbol, side, qty, idem_key=None, contracts=None):
        return "bgx7-native-tpsl"

    def _round_price(self, value, symbol):
        return f"{float(value):.1f}"


class NativeTPSLTests(unittest.IsolatedAsyncioTestCase):
    def _module(self, paper=False, api_key="test-key"):
        return SimpleNamespace(
            PAPER_TRADE=paper,
            API_KEY=api_key,
            to_kucoin=lambda symbol: "XBTUSDTM" if symbol == "BTCUSDT" else symbol,
        )

    def _client_class(self):
        class Client(_FakeClient):
            pass
        hardening.install(Client, self._module(), _Log())
        return Client

    async def test_short_entry_uses_documented_native_tpsl_endpoint(self):
        Client = self._client_class()
        client = Client()

        out = await client.place_order(
            "BTCUSDT", "Sell", 0.002,
            sl=103000, tp=97000,
            idem_key="idem-1", single_submission=True,
        )

        self.assertEqual(out["orderId"], "kc-1")
        self.assertTrue(out["native_tpsl"])
        client._post.assert_awaited_once()
        endpoint, body = client._post.await_args.args[:2]
        self.assertEqual(endpoint, "/api/v1/st-orders")
        self.assertEqual(body["side"], "sell")
        self.assertEqual(body["triggerStopUpPrice"], "103000.0")
        self.assertEqual(body["triggerStopDownPrice"], "97000.0")
        self.assertEqual(body["marginMode"], "CROSS")
        self.assertEqual(body["positionSide"], "BOTH")
        self.assertEqual(body["stopPriceType"], "TP")
        self.assertFalse(body["reduceOnly"])
        self.assertEqual(client.original_calls, [])
        self.assertTrue(client._post.await_args.kwargs["single_attempt"])

    async def test_long_entry_maps_tp_up_and_sl_down(self):
        Client = self._client_class()
        client = Client()

        await client.place_order("BTCUSDT", "Buy", 0.002, sl=99000, tp=104000)
        _, body = client._post.await_args.args[:2]
        self.assertEqual(body["triggerStopUpPrice"], "104000.0")
        self.assertEqual(body["triggerStopDownPrice"], "99000.0")

    async def test_reduce_only_exit_never_uses_tpsl_entry_endpoint(self):
        Client = self._client_class()
        client = Client()

        out = await client.place_order(
            "BTCUSDT", "Buy", 0.002,
            sl=99000, tp=104000, reduce_only=True,
        )

        self.assertEqual(out["orderId"], "original")
        client._post.assert_not_awaited()
        self.assertEqual(len(client.original_calls), 1)

    async def test_paper_mode_preserves_existing_non_mutating_path(self):
        class Client(_FakeClient):
            pass
        hardening.install(Client, self._module(paper=True), _Log())
        client = Client()

        out = await client.place_order("BTCUSDT", "Buy", 0.002, sl=99000, tp=104000)
        self.assertEqual(out["orderId"], "original")
        client._post.assert_not_awaited()

    async def test_ambiguous_response_recovers_by_client_oid_without_resubmit(self):
        Client = self._client_class()
        client = Client()
        client._post.return_value = {"_ambiguous": True}
        client.get_order_by_client_oid.return_value = {
            "orderId": "kc-recovered", "clientOid": "bgx7-native-tpsl"
        }

        out = await client.place_order(
            "BTCUSDT", "Sell", 0.002, sl=103000, tp=97000,
            single_submission=True,
        )

        self.assertEqual(out["orderId"], "kc-recovered")
        self.assertEqual(client._post.await_count, 1)
        client.get_order_by_client_oid.assert_awaited_once_with("bgx7-native-tpsl")


if __name__ == "__main__":
    unittest.main()
