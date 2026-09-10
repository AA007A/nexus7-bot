import unittest
from types import SimpleNamespace

from bot import daily_stop_runtime_hardening as hardening
from bot.config import cfg


class DummyEngine:
    async def _connect(self):
        return None

    async def _update_balance(self):
        return None

    def _check_daily_reset(self):
        self.stop_seen_by_core_reset = self.daily_stop_loss
        return "reset"

    def _update_daily_pnl(self):
        return None


class DailyStopResetOrderingTests(unittest.TestCase):
    def setUp(self):
        self.orig_target = cfg.DAILY_TARGET
        self.orig_target_pct = cfg.DAILY_TARGET_PCT
        self.orig_stop = cfg.DAILY_STOP_LOSS
        self.orig_stop_pct = cfg.DAILY_STOP_LOSS_PCT
        cfg.DAILY_TARGET = 0.0
        cfg.DAILY_TARGET_PCT = 0.01
        cfg.DAILY_STOP_LOSS = 0.0
        cfg.DAILY_STOP_LOSS_PCT = 0.03

    def tearDown(self):
        cfg.DAILY_TARGET = self.orig_target
        cfg.DAILY_TARGET_PCT = self.orig_target_pct
        cfg.DAILY_STOP_LOSS = self.orig_stop
        cfg.DAILY_STOP_LOSS_PCT = self.orig_stop_pct

    def test_core_reset_sees_configured_stop_before_it_logs(self):
        class Engine(DummyEngine):
            pass

        hardening.install(Engine, SimpleNamespace(info=lambda *a, **k: None))
        engine = Engine()
        engine.risk = SimpleNamespace(balance=100.0)
        engine.daily_target = 0.0
        engine.daily_stop_loss = 0.0
        engine.daily_stopped = False
        engine.daily_tracker = SimpleNamespace(
            daily_target=0.0,
            daily_stop_loss=0.0,
            daily_stopped=False,
        )
        engine.stats = SimpleNamespace(daily_pnl=lambda: 0.0)
        engine.positions = {}

        result = engine._check_daily_reset()

        self.assertEqual(result, "reset")
        self.assertEqual(engine.stop_seen_by_core_reset, 3.0)
        self.assertEqual(engine.daily_stop_loss, 3.0)
        self.assertEqual(engine.daily_tracker.daily_stop_loss, 3.0)


if __name__ == "__main__":
    unittest.main()
