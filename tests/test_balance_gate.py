"""Zero is a confirmed account value; a missing response is not zero."""
import unittest
from unittest.mock import AsyncMock
from tests.test_ai_gate import EngineFixture
from bot.kucoin import KuCoinClient


class BalanceGateTests(EngineFixture):
    async def test_BAL01_positive_to_zero(self):
        self.engine.risk.update(0.)
        self.assertEqual(self.engine.risk.balance,0.)
        self.assertFalse(self.engine.risk.can_open(0))

    async def test_BAL02_zero_blocks_open(self):
        self.client.get_balance.return_value=0.
        await self.engine._open(self.sig)
        self.client.place_order.assert_not_awaited()
        self.assertEqual(self.engine.risk.balance,0.)

    async def test_BAL03_failed_query_no_stale_pilot_entry(self):
        self.replace('bot.pilot.PILOT_ENABLED',True)
        self.client.get_balance.side_effect=RuntimeError('offline unavailable')
        await self.engine._open(self.sig)
        self.client.place_order.assert_not_awaited()
        self.assertFalse(self.engine.risk.balance_confirmed)

    async def test_missing_account_value_is_error(self):
        self.client._get=AsyncMock(return_value={})
        with self.assertRaises(RuntimeError):
            await KuCoinClient.get_balance(self.client)

    async def test_zero_account_value_is_valid(self):
        self.client._get=AsyncMock(return_value={'availableBalance':'0'})
        self.assertEqual(await KuCoinClient.get_balance(self.client),0.)

    async def test_last_read_zero_blocks_submission(self):
        self.client.get_balance.side_effect=[100.,0.]
        await self.engine._open(self.sig)
        self.client.place_order.assert_not_awaited()


if __name__=='__main__': unittest.main()
