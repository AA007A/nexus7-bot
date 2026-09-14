import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot import startup_ready_notification as srn


class _Log:
    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass


class StartupReadyNotificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_ready_engine_reports_drawdown_block_truthfully(self):
        engine = SimpleNamespace(
            paper_trade=False,
            connected=True,
            active=True,
            risk=SimpleNamespace(drawdown=0.72),
            pilot=None,
            client=None,
            _validation_safety_lock_active=False,
        )
        notify = AsyncMock()
        with patch.dict(os.environ, {"LIVE_RISK_OVERRIDE_APPROVED": "false"}, clear=False), \
             patch("bot.notifier.notify", notify), \
             patch("bot.startup_ready_notification.asyncio.sleep", AsyncMock()):
            await srn._notify_when_runtime_ready(engine, _Log(), timeout_s=1.0)

        notify.assert_awaited_once()
        msg = notify.await_args.args[0]
        self.assertIn("LIVE BLOQUEADO", msg)
        self.assertIn("DRAWDOWN_HARD_GATE", msg)
        self.assertIn("72.00%", msg)

    async def test_ready_engine_without_blockers_reports_live_operational(self):
        engine = SimpleNamespace(
            paper_trade=False,
            connected=True,
            active=True,
            risk=SimpleNamespace(drawdown=0.0),
            pilot=None,
            client=None,
            _validation_safety_lock_active=False,
        )
        notify = AsyncMock()
        with patch.dict(os.environ, {"LIVE_RISK_OVERRIDE_APPROVED": "false"}, clear=False), \
             patch("bot.notifier.notify", notify), \
             patch("bot.startup_ready_notification.asyncio.sleep", AsyncMock()):
            await srn._notify_when_runtime_ready(engine, _Log(), timeout_s=1.0)

        notify.assert_awaited_once()
        msg = notify.await_args.args[0]
        self.assertIn("LIVE OPERACIONAL", msg)
        self.assertNotIn("LIVE BLOQUEADO", msg)

    async def test_paper_does_not_emit_second_final_live_message(self):
        engine = SimpleNamespace(
            paper_trade=True,
            connected=True,
            active=True,
            _validation_safety_lock_active=False,
        )
        notify = AsyncMock()
        with patch("bot.notifier.notify", notify):
            await srn._notify_when_runtime_ready(engine, _Log(), timeout_s=1.0)
        notify.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
