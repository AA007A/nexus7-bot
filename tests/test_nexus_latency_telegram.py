import asyncio
import inspect
import unittest
from types import SimpleNamespace

from bot import nexus_latency_telegram as nlt


class _Log:
    def info(self, *args, **kwargs):
        pass
    def warning(self, *args, **kwargs):
        pass


class _Notifier:
    def __init__(self):
        self.messages = []
    async def notify(self, text):
        self.messages.append(text)


class _Decision:
    execution_allowed = False
    def to_dict(self):
        return {
            "symbol": "AVAXUSDT",
            "market_regime": "RANGING",
            "risk_reward": 1.54,
            "expected_value": -0.03,
            "reasoning": ["net R:R below minimum"],
        }


class NexusLatencyTelegramTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        nlt._TERMINAL_CACHE.clear()

    async def test_terminal_message_after_nexus_decision(self):
        class Engine:
            async def _nexus_validate(self, sig):
                return _Decision()

        notifier = _Notifier()
        nlt.install(Engine, notifier, _Log())
        decision = await Engine()._nexus_validate(SimpleNamespace(symbol="AVAXUSDT"))
        self.assertIsInstance(decision, _Decision)
        self.assertEqual(len(notifier.messages), 1)
        msg = notifier.messages[0]
        self.assertIn("NEXUS AI — RESULTADO VETO", msg)
        self.assertIn("AVAXUSDT", msg)
        self.assertIn("R:R líquido", msg)
        self.assertIn("EV:", msg)
        self.assertIn("Análise:", msg)
        self.assertIn("execution_effect=NONE", msg)

    async def test_equivalent_terminal_result_is_deduped(self):
        class Engine:
            async def _nexus_validate(self, sig):
                return _Decision()

        notifier = _Notifier()
        nlt.install(Engine, notifier, _Log())
        engine = Engine()
        sig = SimpleNamespace(symbol="AVAXUSDT")
        await engine._nexus_validate(sig)
        await engine._nexus_validate(sig)
        self.assertEqual(len(notifier.messages), 1)

    async def test_failure_is_fail_closed_and_reported(self):
        class Engine:
            async def _nexus_validate(self, sig):
                raise RuntimeError("boom")

        notifier = _Notifier()
        nlt.install(Engine, notifier, _Log())
        with self.assertRaises(RuntimeError):
            await Engine()._nexus_validate(SimpleNamespace(symbol="DOTUSDT"))
        self.assertEqual(len(notifier.messages), 1)
        self.assertIn("ANÁLISE NÃO CONCLUÍDA", notifier.messages[0])
        self.assertIn("nenhuma ordem enviada", notifier.messages[0])

    def test_no_exchange_mutation_calls_added(self):
        src = inspect.getsource(nlt)
        forbidden = (
            ".place_order(", ".cancel_order(", ".cancel_all_orders(",
            ".close_position(", ".set_leverage(", ".set_sl(",
            ".set_position_stops(",
        )
        for token in forbidden:
            self.assertNotIn(token, src)


if __name__ == "__main__":
    unittest.main()
