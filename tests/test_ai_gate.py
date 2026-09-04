"""Adversarial AI boundary tests. The client is always a local spy."""
import asyncio
import os
import unittest
from unittest.mock import AsyncMock, patch

os.environ.update(PAPER_TRADE='false', LIVE_TRADING_CONFIRMED='I_UNDERSTAND_THE_RISK',
                  KUCOIN_API_KEY='offline', KUCOIN_API_SECRET='offline',
                  KUCOIN_API_PASSPHRASE='offline', NEXUS_TELEGRAM='false')
from bot import engine as E
from bot.kucoin import KuCoinClient
from bot.nexus_types import NexusDecision
from bot.strategy import Signal


def approval(symbol='TESTUSDT', allowed=True):
    return NexusDecision(symbol=symbol, decision='LONG', execution_allowed=allowed,
                         confidence=80., setup_quality=80., entry=100.,
                         stop_loss=99., take_profit=103., expected_value=1.,
                         risk_reward=3., reasoning=['offline fixture'])


class EngineFixture(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.patches = []
        self.replace('bot.engine._NEXUS_ENABLED', True)
        self.replace('bot.pilot.PILOT_ENABLED', False)
        self.replace('bot.engine.notify', AsyncMock())
        self.replace('bot.engine.notify_nexus', AsyncMock())
        self.replace('bot.engine.db.save_signal', AsyncMock())
        self.replace('bot.engine.db.save_snapshot', AsyncMock())
        self.replace('bot.engine.scoring.calculate', AsyncMock(return_value={'aprovado':True, 'total':90}))
        self.replace('bot.engine.liq.analyze', lambda **k: type('L', (), {'stop_effective':True, 'liq_move_pct':10., 'stop_move_pct':1.})())
        self.replace('bot.engine.liq.notional_exceeds_tier1', lambda n: False)
        self.client = KuCoinClient()
        self.engine = E.TradingEngine(self.client)
        self.engine.risk.init(100.)
        self.engine.risk.size = lambda *a, **kw: 5.
        self.engine.viable_symbols = ['TESTUSDT', 'OTHERUSDT']
        self.engine.instruments = {s: {'minQty':1, 'lotSize':1, 'qtyStep':1, 'multiplier':1., 'tickSize':.01, 'minNotional':0.} for s in self.engine.viable_symbols}
        self.client._instruments = self.engine.instruments
        self.client.get_balance = AsyncMock(return_value=100.)
        self.client.get_cached_klines = lambda *a: [{'c':100., 'h':101., 'l':99., 'v':1000.}]*200
        self.client.place_order = AsyncMock(return_value={})
        self.engine._nexus_validate = AsyncMock(return_value=approval())
        self.sig = Signal('TESTUSDT', 'LONG', 100., 99., 103., 80., 'offline', 90)
        self.logs = self.replace('bot.engine.log', unittest.mock.Mock())

    def replace(self, name, value):
        p = patch(name, value, create=True); self.patches.append(p); return p.start()

    async def asyncTearDown(self):
        await asyncio.sleep(0)
        await self.client.close()
        for p in reversed(self.patches): p.stop()

    async def attempt(self, decision):
        self.engine._nexus_validate.return_value = decision
        await self.engine._open(self.sig)


class AIGateTests(EngineFixture):
    async def test_AI01_valid_true(self):
        await self.attempt(approval()); self.client.place_order.assert_awaited_once()

    async def test_AI02_false(self):
        await self.attempt(approval(allowed=False)); self.client.place_order.assert_not_awaited()

    async def test_AI03_none(self):
        await self.attempt(None); self.client.place_order.assert_not_awaited()

    async def test_AI04_exception(self):
        self.client.get_funding_rate = AsyncMock(return_value=0.)
        self.client.get_open_interest = AsyncMock(return_value={})
        self.client.get_cached_ticker = lambda s: None
        self.engine._nexus_validate = E.TradingEngine._nexus_validate.__get__(self.engine)
        self.replace('bot.engine.nexus_ai.decide', unittest.mock.Mock(side_effect=RuntimeError('fixture')))
        await self.engine._open(self.sig); self.client.place_order.assert_not_awaited()

    async def test_AI05_timeout(self):
        async def slow(sig): await asyncio.sleep(.1); return approval()
        self.replace('bot.engine._NEXUS_TIMEOUT_S', .01)
        self.engine._nexus_validate.side_effect=slow
        await self.engine._open(self.sig); self.client.place_order.assert_not_awaited()

    async def test_AI06_strings(self):
        for value in ('false', 'true', 'yes', '1', 1):
            with self.subTest(value=value):
                await self.attempt(approval(allowed=value)); self.client.place_order.assert_not_awaited()

    async def test_AI07_malformed(self):
        for value in ({'execution_allowed':True}, '', object()):
            with self.subTest(value=value):
                await self.attempt(value); self.client.place_order.assert_not_awaited()

    async def test_AI08_disabled(self):
        self.replace('bot.engine._NEXUS_ENABLED', False)
        await self.attempt(approval()); self.client.place_order.assert_not_awaited()

    async def test_reject_is_logged(self):
        await self.attempt(approval(allowed=False))
        self.assertTrue(any('[AI_DECISION]' in str(c) and 'REJECT' in str(c) and 'decision_source=nexus_ai' in str(c) for c in self.logs.method_calls))

    async def test_wrong_symbol_and_nonfinite(self):
        for d in (approval('OTHERUSDT'), approval()):
            if d.symbol=='TESTUSDT': d.confidence=float('nan')
            await self.attempt(d); self.client.place_order.assert_not_awaited()


if __name__ == '__main__': unittest.main()
