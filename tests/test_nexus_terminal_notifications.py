import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot import nexus_terminal_notifications as terminal


class _Log:
    info = staticmethod(lambda *a, **k: None)


class _Notifier:
    def __init__(self):
        self.notify = AsyncMock()


class _NexusTypes:
    @staticmethod
    def decision_validation_error(decision, symbol, direction, entry, sl, tp):
        return getattr(decision, "validation_reason", None)


class _Sig(SimpleNamespace):
    pass


class NexusTerminalNotificationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        terminal._terminal_cache.clear()
        self.sig = _Sig(
            symbol="LINKUSDT", direction="SHORT", entry=11.49, sl=11.61, tp=11.26
        )

    async def _drain(self):
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    async def test_reject_emits_final_message(self):
        decision = SimpleNamespace(
            execution_allowed=False,
            setup_quality=72.0,
            confidence=81.0,
            risk_reward=1.47,
            expected_value=0.12,
            reasoning=["R:R líquido 1.47 < mínimo líquido 1.60"],
            validation_reason=None,
        )

        class Engine:
            async def _nexus_validate(self, sig):
                return decision

        notifier = _Notifier()
        terminal.install(Engine, notifier, _NexusTypes, _Log())
        out = await Engine()._nexus_validate(self.sig)
        await self._drain()

        self.assertIs(out, decision)
        notifier.notify.assert_awaited_once()
        text = notifier.notify.await_args.args[0]
        self.assertIn("NEXUS AI — REJEITADO", text)
        self.assertIn("R:R líquido", text)
        self.assertIn("Nenhuma ordem foi enviada", text)

    async def test_approval_does_not_duplicate_existing_core_notification(self):
        decision = SimpleNamespace(
            execution_allowed=True,
            setup_quality=85.0,
            confidence=88.0,
            risk_reward=2.1,
            expected_value=0.5,
            reasoning=["approved"],
            validation_reason=None,
        )

        class Engine:
            async def _nexus_validate(self, sig):
                return decision

        notifier = _Notifier()
        terminal.install(Engine, notifier, _NexusTypes, _Log())
        await Engine()._nexus_validate(self.sig)
        await self._drain()

        notifier.notify.assert_not_awaited()

    async def test_exception_emits_error_and_propagates(self):
        class Engine:
            async def _nexus_validate(self, sig):
                raise RuntimeError("boom")

        notifier = _Notifier()
        terminal.install(Engine, notifier, _NexusTypes, _Log())
        with self.assertRaises(RuntimeError):
            await Engine()._nexus_validate(self.sig)
        await self._drain()

        notifier.notify.assert_awaited_once()
        text = notifier.notify.await_args.args[0]
        self.assertIn("NEXUS AI — ERRO", text)
        self.assertIn("RuntimeError", text)
        self.assertIn("nenhuma ordem foi enviada", text.lower())

    async def test_cancellation_emits_timeout_and_propagates(self):
        class Engine:
            async def _nexus_validate(self, sig):
                raise asyncio.CancelledError()

        notifier = _Notifier()
        terminal.install(Engine, notifier, _NexusTypes, _Log())
        with self.assertRaises(asyncio.CancelledError):
            await Engine()._nexus_validate(self.sig)
        await self._drain()

        notifier.notify.assert_awaited_once()
        text = notifier.notify.await_args.args[0]
        self.assertIn("NEXUS AI — TIMEOUT", text)
        self.assertIn("ai_timeout", text)


if __name__ == "__main__":
    unittest.main()
