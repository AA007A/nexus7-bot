import asyncio
import inspect
import unittest
from types import SimpleNamespace

from bot import nexus_latency_telegram as nlt


class _Log:
    def __init__(self):
        self.warnings = []
    def info(self, *args, **kwargs):
        pass
    def warning(self, *args, **kwargs):
        self.warnings.append(args)
    def debug(self, *args, **kwargs):
        pass


class _Notifier:
    def __init__(self):
        self.messages = []
    async def notify(self, text):
        self.messages.append(text)


class _SlowNotifier(_Notifier):
    def __init__(self, delay):
        super().__init__()
        self.delay = delay
    async def notify(self, text):
        await asyncio.sleep(self.delay)
        self.messages.append(text)


class _ConcurrencyNotifier(_Notifier):
    def __init__(self, delay):
        super().__init__()
        self.delay = delay
        self.active = 0
        self.max_active = 0
    async def notify(self, text):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(self.delay)
            self.messages.append(text)
        finally:
            self.active -= 1


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


class _ApprovedDecision(_Decision):
    execution_allowed = True


class NexusLatencyTelegramTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        pending = getattr(nlt._spawn_notice, "_pending", set())
        for task in list(pending):
            task.cancel()
        if pending:
            await asyncio.gather(*list(pending), return_exceptions=True)
        pending.clear()
        inflight = getattr(nlt._spawn_notice, "_inflight", {})
        inflight.clear()
        if hasattr(nlt._spawn_notice, "_delivery_lock"):
            delattr(nlt._spawn_notice, "_delivery_lock")
        if hasattr(nlt._spawn_notice, "_delivery_lock_loop"):
            delattr(nlt._spawn_notice, "_delivery_lock_loop")

    async def _drain_notices(self):
        for _ in range(20):
            pending = list(getattr(nlt._spawn_notice, "_pending", set()))
            if not pending:
                return
            await asyncio.gather(*pending, return_exceptions=True)

    async def test_terminal_message_after_nexus_decision(self):
        class Engine:
            async def _nexus_validate(self, sig):
                return _Decision()

        notifier = _Notifier()
        nlt.install(Engine, notifier, _Log())
        decision = await Engine()._nexus_validate(SimpleNamespace(symbol="AVAXUSDT"))
        self.assertIsInstance(decision, _Decision)
        await self._drain_notices()
        self.assertEqual(len(notifier.messages), 1)
        msg = notifier.messages[0]
        self.assertIn("NEXUS AI — RESULTADO VETO", msg)
        self.assertIn("AVAXUSDT", msg)
        self.assertIn("R:R líquido", msg)
        self.assertIn("EV:", msg)
        self.assertIn("Análise:", msg)
        self.assertIn("execution_effect=NONE", msg)

    async def test_failure_is_fail_closed_and_reported(self):
        class Engine:
            async def _nexus_validate(self, sig):
                raise RuntimeError("boom")

        notifier = _Notifier()
        nlt.install(Engine, notifier, _Log())
        with self.assertRaises(RuntimeError):
            await Engine()._nexus_validate(SimpleNamespace(symbol="DOTUSDT"))
        await self._drain_notices()
        self.assertEqual(len(notifier.messages), 1)
        self.assertIn("ANÁLISE NÃO CONCLUÍDA", notifier.messages[0])
        self.assertIn("nenhuma ordem enviada", notifier.messages[0])

    async def test_slow_telegram_cannot_turn_finished_pass_into_timeout(self):
        class Engine:
            async def _nexus_validate(self, sig):
                await asyncio.sleep(0.01)
                return _ApprovedDecision()

        notifier = _SlowNotifier(delay=0.20)
        nlt.install(Engine, notifier, _Log())
        decision = await asyncio.wait_for(
            Engine()._nexus_validate(SimpleNamespace(symbol="ATOMUSDT")),
            timeout=0.05,
        )
        self.assertIsInstance(decision, _ApprovedDecision)
        self.assertEqual(notifier.messages, [])
        await self._drain_notices()
        self.assertEqual(len(notifier.messages), 1)
        self.assertIn("RESULTADO APROVADO", notifier.messages[0])
        self.assertNotIn("ANÁLISE NÃO CONCLUÍDA", notifier.messages[0])

    async def test_overlapping_terminal_delivery_for_same_symbol_is_coalesced(self):
        notifier = _SlowNotifier(delay=0.05)
        log = _Log()
        first = nlt._spawn_notice(
            notifier.notify("first"), symbol="DOTUSDT", stage="finished", log=log
        )
        second = nlt._spawn_notice(
            notifier.notify("second"), symbol="DOTUSDT", stage="finished", log=log
        )
        self.assertIs(first, second)
        await self._drain_notices()
        self.assertEqual(notifier.messages, ["first"])
        self.assertEqual(getattr(nlt._spawn_notice, "_inflight", {}), {})

    async def test_different_symbols_are_serialized_not_concurrent(self):
        notifier = _ConcurrencyNotifier(delay=0.02)
        log = _Log()
        symbols = ["DOTUSDT", "ATOMUSDT", "NEARUSDT", "ADAUSDT", "SOLUSDT"]
        tasks = [
            nlt._spawn_notice(
                notifier.notify(symbol), symbol=symbol, stage="finished", log=log
            )
            for symbol in symbols
        ]
        self.assertEqual(len({id(task) for task in tasks}), len(symbols))
        await self._drain_notices()
        self.assertEqual(notifier.max_active, 1)
        self.assertCountEqual(notifier.messages, symbols)
        timeout_warnings = [
            args for args in log.warnings if any("notify_timeout" in str(x) for x in args)
        ]
        self.assertEqual(timeout_warnings, [])

    async def test_queue_wait_does_not_consume_active_delivery_timeout(self):
        notifier = _ConcurrencyNotifier(delay=0.04)
        log = _Log()
        original_timeout = nlt._NOTIFY_TIMEOUT_S
        nlt._NOTIFY_TIMEOUT_S = 0.05
        try:
            for symbol in ("DOTUSDT", "ATOMUSDT", "NEARUSDT", "ADAUSDT"):
                nlt._spawn_notice(
                    notifier.notify(symbol), symbol=symbol, stage="finished", log=log
                )
            await self._drain_notices()
        finally:
            nlt._NOTIFY_TIMEOUT_S = original_timeout
        self.assertEqual(notifier.max_active, 1)
        self.assertEqual(len(notifier.messages), 4)
        timeout_warnings = [
            args for args in log.warnings if any("notify_timeout" in str(x) for x in args)
        ]
        self.assertEqual(timeout_warnings, [])

    async def test_actual_cancellation_emits_only_failure_terminal(self):
        class Engine:
            async def _nexus_validate(self, sig):
                await asyncio.sleep(1.0)
                return _ApprovedDecision()

        notifier = _Notifier()
        nlt.install(Engine, notifier, _Log())
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(
                Engine()._nexus_validate(SimpleNamespace(symbol="ATOMUSDT")),
                timeout=0.01,
            )
        await self._drain_notices()
        self.assertEqual(len(notifier.messages), 1)
        self.assertIn("ANÁLISE NÃO CONCLUÍDA", notifier.messages[0])
        self.assertNotIn("RESULTADO APROVADO", notifier.messages[0])

    def test_notify_timeout_budget_covers_notifier_expected_wait(self):
        self.assertGreaterEqual(nlt._NOTIFY_TIMEOUT_S, 13.0)

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
