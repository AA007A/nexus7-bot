"""Paper must stop every position/order mutation before any HTTP transport."""
import unittest
from unittest.mock import AsyncMock, patch
from tests.test_ai_gate import EngineFixture
from bot.kucoin import KuCoinClient


class PaperMutationTests(EngineFixture):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.replace('bot.kucoin.PAPER_TRADE',True)
        self.client._post=AsyncMock(return_value={'ok':True})
        self.client.get_positions=AsyncMock(return_value=[{'symbol':'TESTUSDT','stopLoss':99.,'takeProfit':103.}])

    async def test_PAPER01_place_order(self):
        result=await KuCoinClient.place_order(self.client,'TESTUSDT','Buy',1.)
        self.client._post.assert_not_awaited()
        self.assertTrue(result['orderId'].startswith('paper_'))

    async def test_PAPER02_set_sl(self):
        await self.client.set_sl('TESTUSDT',99.)
        self.client._post.assert_not_awaited()

    async def test_PAPER03_set_position_stops(self):
        await self.client.set_position_stops('TESTUSDT',sl=99.,tp=103.)
        self.client._post.assert_not_awaited()

    async def test_PAPER04_tp_and_trailing(self):
        await self.client.set_position_stops('TESTUSDT',tp=103.)
        await self.client.set_sl('TESTUSDT',100.5)
        self.client._post.assert_not_awaited()

    async def test_cancel_has_no_transport(self):
        self.client._ensure_session=AsyncMock(side_effect=AssertionError('unexpected transport'))
        await self.client.cancel_all_orders('TESTUSDT')
        self.client._ensure_session.assert_not_awaited()

    async def test_raw_post_has_no_transport(self):
        self.client._ensure_session=AsyncMock(side_effect=AssertionError('unexpected transport'))
        await KuCoinClient._post(self.client,'/api/v1/position/trading-stop',{'symbol':'TESTUSDT'})
        self.client._ensure_session.assert_not_awaited()

    async def test_paper_leverage_mutation_blocked(self):
        with patch.dict('os.environ',KUCOIN_SET_LEVERAGE_ENDPOINT='true'):
            await self.client.set_leverage('TESTUSDT',10)
        self.client._post.assert_not_awaited()

    async def test_live_protection_still_works(self):
        self.replace('bot.kucoin.PAPER_TRADE',False)
        self.assertTrue(await self.client.set_position_stops('TESTUSDT',sl=99.,tp=103.))
        self.client._post.assert_awaited_once()


if __name__=='__main__': unittest.main()
