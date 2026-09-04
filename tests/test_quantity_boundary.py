"""Contracts/base boundaries, including the formerly rejected 0.001 lot."""
import unittest
from unittest.mock import AsyncMock, Mock
from tests.test_ai_gate import EngineFixture
from bot.kucoin import KuCoinClient
from bot.quantity import minimum_base_quantity


class QuantityBoundaryTests(EngineFixture):
    def instrument(self, multiplier):
        self.engine.instruments['TESTUSDT']['multiplier']=multiplier

    async def test_QTY01_multiplier_100(self):
        self.instrument(100)
        self.assertEqual(self.client._round_qty(100,'TESTUSDT'),1)

    async def test_QTY02_multiplier_1(self):
        self.instrument(1)
        self.assertEqual(self.client._round_qty(1,'TESTUSDT'),1)

    async def test_QTY03_multiplier_0001(self):
        self.instrument(.001)
        self.assertEqual(self.client._round_qty(.001,'TESTUSDT'),1)

    async def test_QTY04_exact_minimum_reaches_dispatch(self):
        self.instrument(.001)
        self.engine.risk.size=lambda *a, **kw:.001
        await self.engine._open(self.sig)
        self.client.place_order.assert_awaited_once()
        self.assertEqual(self.client.place_order.call_args.kwargs['qty'],.001)

    async def test_QTY05_min_notional(self):
        info=self.engine.instruments['TESTUSDT']
        info.update(multiplier=.001,minNotional=.35,lotSize=2)
        self.assertEqual(minimum_base_quantity(info,100),.004)
        self.engine.risk.size=lambda *a, **kw:.002
        await self.engine._open(self.sig)
        self.client.place_order.assert_not_awaited()

    async def test_QTY06_conversion_once(self):
        self.instrument(.001)
        self.client._post=AsyncMock(return_value={'orderId':'mock'})
        self.client._round_qty=Mock(wraps=self.client._round_qty)
        await KuCoinClient.place_order(self.client,'TESTUSDT','Buy',.001)
        self.client._round_qty.assert_called_once_with(.001,'TESTUSDT')
        self.assertEqual(self.client._post.call_args.args[1]['size'],'1')

    async def test_round_never_increases_requested_exposure(self):
        self.instrument(.001)
        self.assertEqual(self.client._round_qty(.0019,'TESTUSDT'),1)

    async def test_subminimum_and_bad_metadata_rejected(self):
        self.instrument(.001)
        with self.assertRaises(ValueError): self.client._round_qty(.0001,'TESTUSDT')
        with self.assertRaises((ValueError,KeyError)): self.client._round_qty(1,'MISSING')


if __name__=='__main__': unittest.main()
