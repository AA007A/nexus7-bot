import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from bot.native_stop_repair import set_stops


class NativeStopRepairTests(unittest.IsolatedAsyncioTestCase):
    async def test_delayed_readback_does_not_resubmit(self):
        c = self.client()
        reads = 0
        accepted = []
        async def post(path, body, **kwargs):
            accepted.append(dict(body, isActive=True))
            raise TimeoutError('ack lost')
        async def read(symbol):
            nonlocal reads
            reads += 1
            return accepted if reads >= 3 else []
        c._post = AsyncMock(side_effect=post)
        c.get_stop_orders = AsyncMock(side_effect=read)
        self.assertTrue(await set_stops(c, 'AVAXUSDT', 7.3, 0, self.module(), Mock()))
        c._post.assert_awaited_once()

    def client(self, side="Buy"):
        client = SimpleNamespace(
            get_positions=AsyncMock(return_value=[dict(symbol="AVAXUSDT", size=30, side=side, entryPrice=7.4, markPrice=7.4)]),
            _round_price=lambda price, symbol: str(price),
            get_stop_orders=AsyncMock(return_value=[]),
        )
        async def post(path, body, **kwargs):
            self.assertEqual(path, "/api/v1/orders")
            self.assertTrue(body["closeOrder"])
            self.assertTrue(body["reduceOnly"])
            self.assertNotIn("size", body)
            client.get_stop_orders.return_value = [dict(body, id="native", isActive=True)]
            return {"orderId": "native"}
        client._post = AsyncMock(side_effect=post)
        return client

    def module(self):
        return SimpleNamespace(PAPER_TRADE=False, API_KEY="test", to_kucoin=lambda s: s+"M")

    async def test_long_and_short_native_repair_with_readback(self):
        for side, price, trigger, order_side in (("Buy",7.3,"down","sell"),("Sell",7.5,"up","buy")):
            c = self.client(side)
            self.assertTrue(await set_stops(c, "AVAXUSDT", price, 0, self.module(), Mock()))
            body = c._post.call_args.args[1]
            self.assertEqual((body["stop"],body["side"]),(trigger,order_side))
            c._post.reset_mock()
            self.assertTrue(await set_stops(c, "AVAXUSDT", price, 0, self.module(), Mock()))
            c._post.assert_not_called()

    async def test_acknowledgement_without_readback_fails(self):
        c=self.client()
        c._post=AsyncMock(return_value={"orderId":"ack"})
        self.assertFalse(await set_stops(c,"AVAXUSDT",7.3,0,self.module(),Mock()))

    async def test_unreadable_orders_block_before_post(self):
        c=self.client()
        c.get_stop_orders=AsyncMock(side_effect=TimeoutError())
        self.assertFalse(await set_stops(c,"AVAXUSDT",7.3,0,self.module(),Mock()))
        c._post.assert_not_called()

    async def test_crossed_stop_and_paper_never_submit(self):
        c=self.client()
        self.assertFalse(await set_stops(c,"AVAXUSDT",7.5,0,self.module(),Mock()))
        m=self.module(); m.PAPER_TRADE=True
        self.assertFalse(await set_stops(c,"AVAXUSDT",7.3,0,m,Mock()))
        c._post.assert_not_called()
