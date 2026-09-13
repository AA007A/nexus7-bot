"""Execute actual endpoint bodies with inert engine/task doubles."""
import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import unittest

from bot.service_readiness import engine_task_healthy, evaluate_service_readiness


class HTTPError(Exception):
    def __init__(self, status_code, detail):
        self.status_code = status_code


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    def endpoint(self, state):
        tree = ast.parse((Path(__file__).resolve().parents[1] / 'main.py').read_text())
        fn = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'resume')
        fn.decorator_list = []
        env = dict(app=SimpleNamespace(state=state), Request=object,
                   HTTPException=HTTPError, asyncio=SimpleNamespace(create_task=Mock()))
        exec(compile(ast.Module(body=[fn], type_ignores=[]), 'main.py', 'exec'), env)
        return env['resume'], env['asyncio']

    async def test_resume_cannot_bypass_startup_block(self):
        fn, loop = self.endpoint(SimpleNamespace(blocked=True, ready=True))
        with self.assertRaises(HTTPError):
            await fn(None)
        loop.create_task.assert_not_called()

    async def test_fast_pause_resume_keeps_existing_loop_alive(self):
        task = Mock()
        task.done.return_value = False
        engine = SimpleNamespace(_running=False, active=False)
        fn, loop = self.endpoint(SimpleNamespace(blocked=False, ready=True, engine=engine, engine_task=task))
        await fn(None)
        self.assertTrue(engine._running)
        self.assertTrue(engine.active)
        loop.create_task.assert_not_called()

    async def test_failed_worker_cannot_be_restarted_with_stale_running_flag(self):
        task = Mock()
        task.done.return_value = True
        fn, loop = self.endpoint(SimpleNamespace(blocked=False, ready=True,
            engine=SimpleNamespace(_running=True), engine_task=task))
        with self.assertRaises(HTTPError):
            await fn(None)
        loop.create_task.assert_not_called()

    def test_health_distinguishes_intentional_pause_from_failed_worker(self):
        task = Mock()
        task.done.return_value = True
        task.cancelled.return_value = False
        task.exception.return_value = None
        self.assertTrue(engine_task_healthy(task, running=False))
        self.assertFalse(engine_task_healthy(task, running=True))
        task.exception.return_value = RuntimeError('failed')
        self.assertFalse(engine_task_healthy(task, running=False))
        self.assertFalse(engine_task_healthy(None, running=True))
        r = evaluate_service_readiness(bootstrap_complete=True, startup_blocked=False,
            durable_state_ok=True, instrument_count=1, worker_healthy=False)
        self.assertFalse(r.ready)
        self.assertEqual(r.reason, 'engine_task_unhealthy')
