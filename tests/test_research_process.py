import asyncio
import json
import unittest
from unittest.mock import AsyncMock, Mock, patch
from bot.research_process import run_snapshot


class ResearchProcessTests(unittest.IsolatedAsyncioTestCase):
    async def test_credential_free_child_and_json_result(self):
        proc = Mock(returncode=0)
        proc.communicate = AsyncMock(return_value=(b'{"metadata":{}}', None))
        with patch.dict('os.environ', {'KUCOIN_API_KEY': 'do-not-inherit'}), patch('asyncio.create_subprocess_exec', AsyncMock(return_value=proc)) as spawn:
            self.assertEqual(await run_snapshot({'x': 1}), {'metadata': {}})
            env = spawn.call_args.kwargs['env']
            self.assertNotIn('KUCOIN_API_KEY', env)
            self.assertEqual(env['PAPER_TRADE'], 'true')
            self.assertIn('-S', spawn.call_args.args)
            self.assertEqual(json.loads(proc.communicate.call_args.args[0]), {'x': 1})

    async def test_timeout_and_cancellation_reap_child(self):
        for cancel in (False, True):
            proc = Mock(returncode=None)
            proc.wait = AsyncMock()
            async def blocked(data):
                await asyncio.Future()
            proc.communicate = blocked
            with patch('asyncio.create_subprocess_exec', AsyncMock(return_value=proc)):
                task = asyncio.create_task(run_snapshot({}, timeout=.01))
                if cancel:
                    await asyncio.sleep(0)
                    await asyncio.sleep(0)
                    task.cancel()
                with self.assertRaises(asyncio.CancelledError if cancel else TimeoutError):
                    await task
                proc.kill.assert_called_once()
                proc.wait.assert_awaited_once()

    async def test_real_child_runs_research_without_blocking_loop(self):
        ticks = 0
        async def heartbeat():
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(.005)
        task = asyncio.create_task(heartbeat())
        try:
            result = await run_snapshot(dict(k15=[], k1h=[], k4h=[], symbol='BTCUSDT',
                n_trials=1, execution_context=dict(taker_fee_rate=.0006,
                fee_source='test', slippage_rate=.0001, funding_events=[])), timeout=30)
            self.assertFalse(result['metadata']['promotion_allowed'])
            self.assertGreater(ticks, 1)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_invalid_output_is_not_promoted(self):
        proc = Mock(returncode=0, communicate=AsyncMock(return_value=(b'[]', None)))
        with patch('asyncio.create_subprocess_exec', AsyncMock(return_value=proc)):
            with self.assertRaises(ValueError):
                await run_snapshot({})
