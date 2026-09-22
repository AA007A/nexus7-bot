"""Pilot sizing must ignore a larger normal risk allocation."""
import time
import unittest
from unittest.mock import patch
from tests.test_ai_gate import EngineFixture
from bot.pilot import PILOT_RELEASE_TOKEN
from bot.config import cfg
from bot.kucoin import TAKER_FEE


class PilotFixture(EngineFixture):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.replace('bot.pilot.PILOT_ENABLED', True)
        self.account = patch.dict(
            'os.environ',
            PILOT_ACCOUNT_CONFIRMED='true',
            PILOT_RELEASE_APPROVED=PILOT_RELEASE_TOKEN,
        )
        self.account.start()
        self.client._last_ws_update = time.time()
        self.client._order_registry = self.engine.orders  # empty but initialized
        # Core pilot fixtures do not install the controlled-LIVE wrapper, so
        # model its explicit collateral authority directly. Equity remains in
        # risk.balance; buying power lives here even when numerically equal.
        self.engine._pilot_available_balance = 100.0

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
        self.engine._pilot_available_balance = .001
        await self.engine._open(self.sig)
        self.client.place_order.assert_not_awaited()

    def _minimum_required_collateral(self):
        return 1.0 * self.sig.entry * (1.0 / cfg.LEVERAGE + TAKER_FEE)

    async def test_pilot_equity_can_exceed_available_margin_when_collateral_sufficient(self):
        self.engine._pilot_available_balance = self._minimum_required_collateral() + 1.0
        await self.engine._open(self.sig)
        self.client.place_order.assert_awaited_once()

    async def test_pilot_zero_collateral_fails_closed(self):
        self.engine._pilot_available_balance = 0.0
        await self.engine._open(self.sig)
        self.client.place_order.assert_not_awaited()

    async def test_pilot_collateral_just_below_requirement_blocks(self):
        self.engine._pilot_available_balance = self._minimum_required_collateral() - 1e-9
        await self.engine._open(self.sig)
        self.client.place_order.assert_not_awaited()

    async def test_pilot_collateral_exactly_at_requirement_passes(self):
        self.engine._pilot_available_balance = self._minimum_required_collateral()
        await self.engine._open(self.sig)
        self.client.place_order.assert_awaited_once()

    async def test_pilot_collateral_just_above_requirement_passes(self):
        self.engine._pilot_available_balance = self._minimum_required_collateral() + 1e-9
        await self.engine._open(self.sig)
        self.client.place_order.assert_awaited_once()


if __name__ == '__main__': unittest.main()
