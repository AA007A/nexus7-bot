import unittest
from types import SimpleNamespace
from unittest.mock import patch

from bot import daily_stop_runtime_hardening as hardening
from bot.daily_tracker import DailyTracker
from bot.config import cfg


class PaperDailyStopAdvisoryTests(unittest.TestCase):
    def setUp(self):
        self.orig_stop = cfg.DAILY_STOP_LOSS
        self.orig_stop_pct = cfg.DAILY_STOP_LOSS_PCT
        self.orig_target = cfg.DAILY_TARGET
        self.orig_target_pct = cfg.DAILY_TARGET_PCT
        cfg.DAILY_STOP_LOSS = 3.0
        cfg.DAILY_STOP_LOSS_PCT = 0.03
        cfg.DAILY_TARGET = 0.0
        cfg.DAILY_TARGET_PCT = 0.05

    def tearDown(self):
        cfg.DAILY_STOP_LOSS = self.orig_stop
        cfg.DAILY_STOP_LOSS_PCT = self.orig_stop_pct
        cfg.DAILY_TARGET = self.orig_target
        cfg.DAILY_TARGET_PCT = self.orig_target_pct

    @staticmethod
    def _engine_class():
        class Engine:
            async def _connect(self):
                return None

            async def _update_balance(self):
                return None

            def _check_daily_reset(self):
                return None

            def _update_daily_pnl(self):
                result = self.daily_tracker.check_limits()
                if result in ("STOP", "WEEKLY_STOP", "MONTHLY_STOP"):
                    self.daily_stopped = True
                return result

        return Engine

    def _build(self, Engine):
        engine = Engine()
        engine.risk = SimpleNamespace(balance=100.0)
        engine.daily_target = 5.0
        engine.daily_stop_loss = 3.0
        engine.daily_stopped = False
        engine.daily_tracker = DailyTracker()
        engine.daily_tracker.daily_stop_loss = 3.0
        engine.stats = SimpleNamespace(daily_pnl=lambda: -4.0)
        engine.positions = {}
        return engine

    def test_paper_daily_stop_is_advisory_and_does_not_block(self):
        Engine = self._engine_class()
        hardening.install(Engine, SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None))
        engine = self._build(Engine)

        with patch("bot.kucoin.PAPER_TRADE", True):
            result = engine._update_daily_pnl()

        self.assertEqual(result, "OK")
        self.assertFalse(engine.daily_stopped)
        self.assertFalse(engine.daily_tracker.daily_stopped)
        self.assertEqual(engine.daily_tracker.daily_stop_loss, 3.0)

    def test_live_daily_stop_remains_blocking(self):
        Engine = self._engine_class()
        hardening.install(Engine, SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None))
        engine = self._build(Engine)

        with patch("bot.kucoin.PAPER_TRADE", False):
            result = engine._update_daily_pnl()

        self.assertEqual(result, "STOP")
        self.assertTrue(engine.daily_stopped)
        self.assertTrue(engine.daily_tracker.daily_stopped)

    def test_paper_weekly_stop_still_blocks(self):
        Engine = self._engine_class()
        hardening.install(Engine, SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None))
        engine = self._build(Engine)
        engine.daily_tracker.weekly_stop_loss = 3.0
        engine.daily_tracker.weekly_pnl = -4.0

        with patch("bot.kucoin.PAPER_TRADE", True):
            result = engine._update_daily_pnl()

        self.assertEqual(result, "WEEKLY_STOP")
        self.assertTrue(engine.daily_stopped)
        self.assertTrue(engine.daily_tracker.weekly_stopped)
        self.assertFalse(engine.daily_tracker.daily_stopped)


if __name__ == "__main__":
    unittest.main()
