import unittest
from types import SimpleNamespace

from bot import daily_stop_runtime_hardening as hardening


class DummyLog:
    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass


class BaseEngine:
    async def _connect(self):
        return None

    async def _update_balance(self):
        return None

    def _check_daily_reset(self):
        return None

    def _update_daily_pnl(self):
        self.daily_stopped = True
        self.daily_tracker.daily_stopped = True
        return "stop"


class DailyStopAdvisoryIsolationTests(unittest.TestCase):
    def _engine(self, *, paper=False, shadow=False):
        class Engine(BaseEngine):
            pass

        hardening.install(Engine, DummyLog())
        engine = Engine()
        engine.paper_trade = paper
        engine._validation_safety_lock_active = shadow
        engine.daily_stopped = False
        engine.daily_target = 1.0
        engine.daily_stop_loss = 1.0
        engine.risk = SimpleNamespace(balance=0.0)
        engine.stats = SimpleNamespace(daily_pnl=lambda: -2.0)
        engine.positions = {}
        engine.daily_tracker = SimpleNamespace(
            daily_target=1.0,
            daily_stop_loss=1.0,
            daily_stopped=False,
            weekly_stopped=True,
            monthly_stopped=False,
            daily_pnl=0.0,
        )
        return engine

    def test_live_daily_stop_remains_hard(self):
        engine = self._engine(paper=False, shadow=False)
        result = engine._update_daily_pnl()
        self.assertEqual(result, "stop")
        self.assertTrue(engine.daily_stopped)
        self.assertTrue(engine.daily_tracker.daily_stopped)

    def test_paper_daily_stop_is_advisory(self):
        engine = self._engine(paper=True, shadow=False)
        result = engine._update_daily_pnl()
        self.assertEqual(result, "stop")
        self.assertFalse(engine.daily_stopped)
        self.assertFalse(engine.daily_tracker.daily_stopped)
        self.assertTrue(engine.daily_tracker.weekly_stopped)

    def test_shadow_daily_stop_is_advisory(self):
        engine = self._engine(paper=False, shadow=True)
        result = engine._update_daily_pnl()
        self.assertEqual(result, "stop")
        self.assertFalse(engine.daily_stopped)
        self.assertFalse(engine.daily_tracker.daily_stopped)
        self.assertTrue(engine.daily_tracker.weekly_stopped)


if __name__ == "__main__":
    unittest.main()
