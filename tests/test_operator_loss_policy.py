import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from bot.operator_loss_policy import stop_price, install


class LossPolicyTests(unittest.IsolatedAsyncioTestCase):
    def test_opportunity_cost_is_decision_snapshot(self):
        from bot.missed_opportunity_audit import _decision_cost_pct, _persisted_cost_pct
        decision = SimpleNamespace(_bgx_nexus_cost_context=SimpleNamespace(taker_fee=.0006, slippage=.0001))
        self.assertAlmostEqual(_decision_cost_pct(decision, "BTCUSDT"), .14)
        self.assertAlmostEqual(_persisted_cost_pct('{"estimated_round_trip_cost_pct":0.14}', "BTCUSDT"), .14)

    def test_latest_derivatives_source_not_first_row(self):
        from bot.derivatives_news_freshness_hardening import _latest_source_row
        rows = [{"timestamp": 1000, "buySellRatio": 1}, {"timestamp": 2000, "buySellRatio": 2}]
        self.assertEqual(_latest_source_row(rows)["buySellRatio"], 2)
        self.assertEqual(_latest_source_row(list(reversed(rows)))["buySellRatio"], 2)

    async def test_stop_reaches_normal_entry_gate(self):
        from unittest.mock import patch
        from bot.config import cfg
        calls = []
        class Engine:
            async def _open(self, sig):
                calls.append(sig.sl)
                return "normal_gate"
            _check_stagnation_and_invalidation = AsyncMock()
        log = SimpleNamespace(info=lambda *a: None, error=lambda *a: None)
        install(Engine, log)
        engine = Engine()
        engine.paper_trade = False
        engine.pilot = SimpleNamespace(enabled=True)
        sig = SimpleNamespace(symbol="ETHUSDT", entry=2500., direction="LONG", sl=2499., tp=2600.)
        with patch.object(cfg, "LEVERAGE", 50):
            self.assertEqual(await engine._open(sig), "normal_gate")
        self.assertLess(calls[0], 2499.)
        self.assertAlmostEqual(sig.rr, 100 / (2500 - calls[0]))

    def test_long_short_cost_budget(self):
        for side in ("LONG", "SHORT"):
            stop = stop_price(2500., side, 50., .0022)
            self.assertAlmostEqual((abs(stop - 2500) / 2500 + .0022) * 50, .5)
        with self.assertRaises(ValueError):
            stop_price(2500., "LONG", 50., .02)

    def test_margin_rounds_down(self):
        from bot.pilot_live_runtime import _pilot_quantity_for_notional
        qty = _pilot_quantity_for_notional(dict(multiplier=.01, lotSize=1, minQty=1, minNotional=0), 2531.41, 23.0667 * .5 * 50)
        self.assertEqual(qty, .22)
        self.assertLessEqual(qty * 2531.41 / 50, 23.0667 * .5)

    async def test_live_discretionary_exit_disabled_paper_retained(self):
        original = AsyncMock()
        class Engine:
            _open = AsyncMock()
            _check_stagnation_and_invalidation = original
        install(Engine, SimpleNamespace())
        engine = Engine()
        engine.paper_trade = False
        engine.pilot = SimpleNamespace(enabled=True)
        await engine._check_stagnation_and_invalidation()
        original.assert_not_called()
        engine.paper_trade = True
        await engine._check_stagnation_and_invalidation()
        original.assert_awaited_once()
