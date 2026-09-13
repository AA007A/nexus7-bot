import json
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot import durable_daily_stop as gate


class Stats:
    def __init__(self, durable=None):
        self.trades = []
        self._durable_daily_pnl = durable or {}


class DailyStopTests(unittest.IsolatedAsyncioTestCase):
    def live_engine(self, *, stopped=False, realized=0.0, unrealized=0.0, limit=1.0, pnl_ok=True):
        day = '2026-09-13'
        durable = {day: {}}
        if realized:
            durable[day]['a' * 64] = {
                'pnl': realized,
                'source': 'TEST',
                'closed_at': '2026-09-13T12:00:00+00:00',
            }
        return SimpleNamespace(
            daily_stopped=stopped,
            daily_tracker=SimpleNamespace(daily_stopped=stopped, daily_stop_loss=limit),
            daily_stop_loss=limit,
            _daily_pnl_ok=pnl_ok,
            stats=Stats(durable),
            positions={'BTCUSDT': SimpleNamespace(pnl=unrealized)} if unrealized else {},
        )

    async def test_v2_namespace_is_retired_and_does_not_fabricate_stop(self):
        env = {
            'RAILWAY_PROJECT_ID': 'c3ffa9f5-8c64-4859-a722-a07105ba5e84',
            'RAILWAY_SERVICE_ID': '751b41ee-2aef-4487-b19a-f305f15c64fe',
            'RAILWAY_ENVIRONMENT_ID': '3f436900-ab27-4b41-9248-1a9a9f8dc80c',
        }
        with patch.dict('os.environ', env), \
             patch.object(gate.db, '_is_pg', True), \
             patch.object(gate.db, 'configured_postgres_unavailable', return_value=False), \
             patch.object(gate.db, 'load_key_value', new_callable=AsyncMock, return_value=None) as read, \
             patch.object(gate.db, 'save_key_value', new_callable=AsyncMock, return_value=True) as write:
            engine = self.live_engine(stopped=False)
            self.assertFalse(await gate.entries_blocked(engine, datetime(2026, 9, 13, tzinfo=timezone.utc)))
            self.assertFalse(engine.daily_stopped)
            loaded_key = read.await_args.args[0]
            self.assertTrue(loaded_key.startswith('daily_stop_v3:'))
            write.assert_not_awaited()

    async def test_unproven_stale_flag_is_cleared_and_does_not_block(self):
        day = datetime(2026, 9, 13, tzinfo=timezone.utc)
        engine = self.live_engine(stopped=True, realized=0.0, unrealized=0.0, limit=1.0)
        with patch.dict('os.environ', {'RAILWAY_SERVICE_ID': ''}), \
             patch.object(gate.db, 'configured_postgres_unavailable', return_value=False), \
             patch.object(gate.db, 'load_key_value', new_callable=AsyncMock, return_value=None), \
             patch.object(gate.db, 'save_key_value', new_callable=AsyncMock, return_value=True) as write:
            self.assertFalse(await gate.entries_blocked(engine, day))
            self.assertFalse(engine.daily_stopped)
            self.assertFalse(engine.daily_tracker.daily_stopped)
            write.assert_not_awaited()

    async def test_legitimate_breach_persists_evidence_and_restores(self):
        memory = {}

        async def read(key, **kw):
            return memory.get(key)

        async def write(key, value, **kw):
            memory[key] = value
            return True

        day = datetime(2026, 9, 13, tzinfo=timezone.utc)
        with patch.dict('os.environ', {'RAILWAY_SERVICE_ID': ''}), \
             patch.object(gate.db, 'configured_postgres_unavailable', return_value=False), \
             patch.object(gate.db, 'load_key_value', side_effect=read), \
             patch.object(gate.db, 'save_key_value', side_effect=write):
            first = self.live_engine(stopped=True, realized=-1.2, limit=1.0)
            self.assertTrue(await gate.entries_blocked(first, day))
            self.assertEqual(len(memory), 1)
            state = json.loads(next(iter(memory.values())))
            self.assertEqual(state['version'], 3)
            self.assertEqual(state['source'], 'RUNTIME_DAILY_STOP')
            self.assertLessEqual(state['trigger_pnl'], -state['stop_limit'])

            restart = self.live_engine(stopped=False, realized=0.0, limit=1.0)
            self.assertTrue(await gate.entries_blocked(restart, day))
            self.assertTrue(restart.daily_stopped)
            self.assertTrue(restart.daily_tracker.daily_stopped)

            restart.daily_stopped = restart.daily_tracker.daily_stopped = False
            self.assertFalse(await gate.entries_blocked(
                restart, datetime(2026, 9, 14, tzinfo=timezone.utc)))

    async def test_combined_realized_and_unrealized_can_prove_breach(self):
        day = datetime(2026, 9, 13, tzinfo=timezone.utc)
        engine = self.live_engine(stopped=True, realized=-0.4, unrealized=-0.7, limit=1.0)
        with patch.dict('os.environ', {'RAILWAY_SERVICE_ID': ''}), \
             patch.object(gate.db, 'configured_postgres_unavailable', return_value=False), \
             patch.object(gate.db, 'load_key_value', new_callable=AsyncMock, return_value=None), \
             patch.object(gate.db, 'save_key_value', new_callable=AsyncMock, return_value=True) as write:
            self.assertTrue(await gate.entries_blocked(engine, day))
            state = json.loads(write.await_args.args[1])
            self.assertAlmostEqual(state['trigger_pnl'], -1.1)

    async def test_unconfirmed_pnl_fails_closed_without_persisting(self):
        day = datetime(2026, 9, 13, tzinfo=timezone.utc)
        engine = self.live_engine(stopped=True, pnl_ok=False)
        with patch.dict('os.environ', {'RAILWAY_SERVICE_ID': ''}), \
             patch.object(gate.db, 'configured_postgres_unavailable', return_value=False), \
             patch.object(gate.db, 'load_key_value', new_callable=AsyncMock, return_value=None), \
             patch.object(gate.db, 'save_key_value', new_callable=AsyncMock, return_value=True) as write:
            self.assertTrue(await gate.entries_blocked(engine, day))
            write.assert_not_awaited()

    async def test_malformed_or_unproven_v3_state_fails_closed(self):
        bad = json.dumps({
            'version': 3,
            'day': '2026-09-13',
            'trigger_pnl': 0.0,
            'stop_limit': 1.0,
            'triggered_at': '2026-09-13T12:00:00+00:00',
            'source': 'RUNTIME_DAILY_STOP',
        })
        engine = self.live_engine()
        with patch.dict('os.environ', {'RAILWAY_SERVICE_ID': ''}), \
             patch.object(gate.db, 'configured_postgres_unavailable', return_value=False), \
             patch.object(gate.db, 'load_key_value', new_callable=AsyncMock, return_value=bad):
            self.assertTrue(await gate.entries_blocked(
                engine, datetime(2026, 9, 13, tzinfo=timezone.utc)))
            self.assertFalse(engine.daily_stopped)

    async def test_storage_failure_blocks_without_fabricating_stop(self):
        engine = self.live_engine()
        with patch.object(gate.db, 'configured_postgres_unavailable', return_value=True):
            self.assertTrue(await gate.entries_blocked(engine))
            self.assertFalse(engine.daily_stopped)

    async def test_paper_and_shadow_do_not_access_storage(self):
        with patch.object(gate.db, 'load_key_value', new_callable=AsyncMock) as read:
            for engine in (
                SimpleNamespace(paper_trade=True),
                SimpleNamespace(_validation_safety_lock_active=True),
            ):
                self.assertFalse(await gate.entries_blocked(engine))
            read.assert_not_called()

    def test_key_stable_across_deployments_and_uses_v3(self):
        with patch.dict('os.environ', {'RAILWAY_DEPLOYMENT_ID': 'one'}):
            first = gate.state_key('2026-09-13')
        with patch.dict('os.environ', {'RAILWAY_DEPLOYMENT_ID': 'two'}):
            self.assertEqual(first, gate.state_key('2026-09-13'))
        self.assertTrue(first.startswith('daily_stop_v3:'))


if __name__ == '__main__':
    unittest.main()
