import os
import tempfile
import unittest
from types import SimpleNamespace

from bot import daily_stop_runtime_hardening as daily_runtime
from bot import selfcheck_entrypoint_hardening as selfcheck_hardening


class _Log:
    def info(self, *args, **kwargs):
        return None


class _Tracker:
    def __init__(self):
        self.daily_target = 0.0
        self.daily_stop_loss = 0.0
        self.daily_pnl = 0.0
        self.daily_stopped = False


class _Stats:
    def daily_pnl(self):
        return -0.20


class _FakeEngine:
    def __init__(self):
        self.daily_target = 1.0
        self.daily_stop_loss = 0.60
        self.daily_stopped = False
        self.daily_tracker = _Tracker()
        self.stats = _Stats()
        self.positions = {
            "ETHUSDT": SimpleNamespace(pnl=-0.45),
            "SOLUSDT": SimpleNamespace(pnl=0.05),
        }
        self.seen_pnl = None
        self.seen_stop = None

    async def _connect(self):
        return "connected"

    async def _update_balance(self):
        self.daily_target = 1.25
        self.daily_stop_loss = 0.75
        return "updated"

    def _check_daily_reset(self):
        self.daily_stopped = False
        return "reset"

    def _update_daily_pnl(self):
        self.seen_pnl = self.daily_tracker.daily_pnl
        self.seen_stop = self.daily_tracker.daily_stop_loss
        if self.daily_tracker.daily_pnl <= -self.daily_tracker.daily_stop_loss:
            self.daily_tracker.daily_stopped = True
            self.daily_stopped = True
        return "checked"


class DailyStopRuntimeHardeningTests(unittest.IsolatedAsyncioTestCase):
    async def test_tracker_receives_engine_limits_and_current_total_pnl(self):
        daily_runtime.install(_FakeEngine, _Log())
        engine = _FakeEngine()

        self.assertEqual(await engine._connect(), "connected")
        self.assertEqual(engine.daily_tracker.daily_stop_loss, 0.60)

        self.assertEqual(await engine._update_balance(), "updated")
        self.assertEqual(engine.daily_tracker.daily_target, 1.25)
        self.assertEqual(engine.daily_tracker.daily_stop_loss, 0.75)

        # realized=-0.20, unrealized=-0.40 => current daily PnL=-0.60
        engine.daily_stop_loss = 0.50
        self.assertEqual(engine._update_daily_pnl(), "checked")
        self.assertAlmostEqual(engine.seen_pnl, -0.60, places=7)
        self.assertAlmostEqual(engine.seen_stop, 0.50, places=7)
        self.assertTrue(engine.daily_stopped)


class SelfcheckEntrypointHardeningTests(unittest.TestCase):
    def test_python_m_entrypoint_is_not_reported_as_orphan(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "ci_deploy_gate.py")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(
                    "def main():\n"
                    "    return 0\n\n"
                    "if __name__ == '__main__':\n"
                    "    raise SystemExit(main())\n"
                )

            fake_selfcheck = SimpleNamespace(
                check_orphan_modules=lambda paths: [
                    "ci_deploy_gate.py (10 linhas): não é importado por nenhum outro módulo do projeto/entrypoint — código morto"
                ]
            )
            selfcheck_hardening.install(fake_selfcheck, _Log())
            self.assertEqual(fake_selfcheck.check_orphan_modules([path]), [])


if __name__ == "__main__":
    unittest.main()
