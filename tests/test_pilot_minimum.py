"""Pilot sizing must ignore a larger normal risk allocation."""
import time
import unittest
from unittest.mock import patch
from tests.test_ai_gate import EngineFixture


class PilotFixture(EngineFixture):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.replace('bot.pilot.PILOT_ENABLED', True)
        self.account = patch.dict('os.environ', PILOT_ACCOUNT_CONFIRMED='true')
        self.account.start()
        self.client._last_ws_update = time.time()
        self.client._order_registry = self.engine.orders  # empty but initialized

    async def asyncTearDown(self):
        self.account.stop()
        await super().asyncTearDown()


class PilotMinimumTests(PilotFixture):
    async def test_PILOT01_minimum_order(self):
        await self.engine._open(self.sig)
        self.client.place_order.assert_awaited_once()
        self.assertEqual(self.client.place_order.call_args.kwargs['qty'], 1.)

    async def test_PILOT02_risk_size_cannot_increase_pilot(self):
        self.engine.risk.size = lambda *a, **kw: 1000.
        await self.engine._open(self.sig)
        self.assertEqual(self.client.place_order.call_args.kwargs['qty'], 1.)

    async def test_pilot_lot_and_quote_minimum(self):
        self.engine.instruments['TESTUSDT'].update(lotSize=2, minQty=3, minNotional=350)
        await self.engine._open(self.sig)
        self.assertEqual(self.client.place_order.call_args.kwargs['qty'], 4.)

    async def test_pilot_minimum_must_fit_available_margin(self):
        self.client.get_balance.return_value=.001
        await self.engine._open(self.sig)
        self.client.place_order.assert_not_awaited()


if __name__ == '__main__': unittest.main()
