import asyncio
import logging
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
        terminal._ai_telemetry_cache.clear()
        self.sig = _Sig(
            symbol="LINKUSDT", direction="SHORT", entry=11.49, sl=11.61, tp=11.26,
            score=72,
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
        self.assertIn("Score candidato: `72.0/100`", text)
        self.assertIn("Score NEXUS: `72.0`", text)
        self.assertIn("R:R líquido: `1.47`", text)
        self.assertIn("EV: `+0.120%`", text)
        self.assertIn("Nenhuma ordem foi enviada", text)

    async def test_early_rr_veto_never_reports_fake_zero_metrics(self):
        decision = SimpleNamespace(
            execution_allowed=False,
            setup_quality=0.0,
            confidence=0.0,
            risk_reward=0.0,
            expected_value=0.0,
            reasoning=[
                "Regime: TRENDING_BEAR",
                "Ensemble: SHORT conf=81.0 (HIGH) | modelos=5",
                "R:R líquido 1.33 < mínimo líquido 1.60 (bruto exigido: 2.0)",
            ],
            validation_reason=None,
        )

        class Engine:
            async def _nexus_validate(self, sig):
                return decision

        notifier = _Notifier()
        terminal.install(Engine, notifier, _NexusTypes, _Log())
        await Engine()._nexus_validate(self.sig)
        await self._drain()

        text = notifier.notify.await_args.args[0]
        self.assertIn("Score candidato: `72.0/100`", text)
        self.assertIn("Score NEXUS: `não calculado (veto anterior ao score final)`", text)
        self.assertIn("Confiança: `81.0%`", text)
        self.assertIn("R:R líquido: `1.33`", text)
        self.assertIn("EV: `—`", text)
        self.assertNotIn("Score final: `0.0/100`", text)
        self.assertNotIn("R:R líquido: `0.00`", text)
        self.assertNotIn("EV: `+0.000%`", text)

    async def test_early_rr_veto_rewrites_ai_decision_log_truthfully(self):
        decision = SimpleNamespace(
            execution_allowed=False,
            setup_quality=0.0,
            confidence=0.0,
            risk_reward=0.0,
            expected_value=0.0,
            reasoning=[
                "Regime: TRENDING_BEAR",
                "Ensemble: SHORT conf=81.0 (HIGH) | modelos=5",
                "R:R líquido 1.33 < mínimo líquido 1.60 (bruto exigido: 2.0)",
            ],
            validation_reason=None,
        )
        terminal._cache_ai_telemetry(self.sig, decision, None)
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname=__file__, lineno=1,
            msg=(
                "[AI_DECISION] symbol=LINKUSDT side=SHORT decision=REJECT "
                "approved=False decision_source=nexus_ai score=0.0 confidence=0.0 "
                "ts=1789090209 reason=ai_veto"
            ),
            args=(), exc_info=None,
        )

        self.assertTrue(terminal._TruthfulAIDecisionFilter().filter(record))
        text = record.getMessage()
        self.assertIn("candidate_score=72.0", text)
        self.assertIn("nexus_score=N/A", text)
        self.assertIn("confidence=81.0", text)
        self.assertIn("rr_net=1.33", text)
        self.assertIn("ev=N/A", text)
        self.assertIn("stage=EV_RR_GATE", text)
        self.assertIn("reason=R:R líquido 1.33 < mínimo líquido 1.60", text)
        self.assertNotIn("score=0.0 confidence=0.0", text)

    async def test_validation_failure_ai_decision_log_is_not_rewritten(self):
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname=__file__, lineno=1,
            msg=(
                "[AI_DECISION] symbol=LINKUSDT side=SHORT decision=REJECT "
                "approved=False decision_source=validation_failure score=N/A "
                "confidence=N/A ts=1789090209 reason=invalid_decision_type"
            ),
            args=(), exc_info=None,
        )
        original = record.getMessage()
        self.assertTrue(terminal._TruthfulAIDecisionFilter().filter(record))
        self.assertEqual(record.getMessage(), original)

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
