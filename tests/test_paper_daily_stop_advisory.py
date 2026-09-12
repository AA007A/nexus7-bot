import unittest
from types import SimpleNamespace

from bot import daily_stop_runtime_hardening as hardening
from bot import kucoin


class DummyEngine:
    async def _connect(self):
        return None

    async def _update_balance(self):
        return None

    def _check_daily_reset(self):
        self.daily_stopped = False
        self.daily_tracker.daily_stopped = False
        return "reset"

    def _update_daily_pnl(self):
        # Model the core limit behavior: emit/latched only once while the
        # tracker says the daily stop has not already fired.
        if self.daily_tracker.daily_pnl <= -self.daily_tracker.daily_stop_loss:
            if not self.daily_tracker.daily_stopped:
                self.core_stop_events += 1
                self.daily_tracker.daily_stopped = True
                self.daily_stopped = True
        return "updated"


class PaperDailyStopAdvisoryTests(unittest.TestCase):
    def setUp(self):
        self.orig_paper = kucoin.PAPER_TRADE

    def tearDown(self):
        kucoin.PAPER_TRADE = self.orig_paper

    def _engine(self):
        class Engine(DummyEngine):
            pass

        logs = []
        hardening.install(
            Engine,
            SimpleNamespace(
                info=lambda *a, **k: None,
                warning=lambda *a, **k: logs.append((a, k)),
            ),
        )
        engine = Engine()
        engine.risk = SimpleNamespace(balance=100.0)
        engine.daily_target = 5.0
        engine.daily_stop_loss = 3.0
        engine.daily_stopped = False
        engine.daily_tracker = SimpleNamespace(
            daily_pnl=0.0,
            daily_target=5.0,
            daily_stop_loss=3.0,
            daily_stopped=False,
            weekly_stopped=False,
            monthly_stopped=False,
            _last_reset_day=12,
        )
        engine.stats = SimpleNamespace(daily_pnl=lambda: -4.0)
        engine.positions = {}
        engine.core_stop_events = 0
        return engine, logs

    def test_paper_daily_stop_becomes_advisory_but_tracker_stays_latched(self):
        kucoin.PAPER_TRADE = True
        engine, logs = self._engine()

        engine._update_daily_pnl()

        self.assertFalse(engine.daily_stopped)
        self.assertTrue(engine.daily_tracker.daily_stopped)
        self.assertEqual(engine.core_stop_events, 1)
        self.assertEqual(len(logs), 1)

        # A second loop must continue PAPER validation without re-emitting the
        # stop event or clearing its tracker evidence.
        engine._update_daily_pnl()
        self.assertFalse(engine.daily_stopped)
        self.assertTrue(engine.daily_tracker.daily_stopped)
        self.assertEqual(engine.core_stop_events, 1)
        self.assertEqual(len(logs), 1)

    def test_live_daily_stop_remains_hard(self):
        kucoin.PAPER_TRADE = False
        engine, logs = self._engine()

        engine._update_daily_pnl()

        self.assertTrue(engine.daily_stopped)
        self.assertTrue(engine.daily_tracker.daily_stopped)
        self.assertEqual(engine.core_stop_events, 1)
        self.assertEqual(logs, [])

    def test_weekly_stop_is_not_bypassed_even_in_paper(self):
        kucoin.PAPER_TRADE = True
        engine, logs = self._engine()
        engine.daily_tracker.weekly_stopped = True
        engine.daily_tracker.daily_stopped = True
        engine.daily_stopped = True

        engine._update_daily_pnl()

        self.assertTrue(engine.daily_stopped)
        self.assertTrue(engine.daily_tracker.weekly_stopped)
        self.assertEqual(logs, [])


if __name__ == "__main__":
    unittest.main()
