import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from bot import durable_daily_stop as gate


class DailyStopTests(unittest.IsolatedAsyncioTestCase):
    async def test_observed_stop_migration_is_scoped_and_expires(self):
        env = {'RAILWAY_PROJECT_ID':'c3ffa9f5-8c64-4859-a722-a07105ba5e84',
               'RAILWAY_SERVICE_ID':'751b41ee-2aef-4487-b19a-f305f15c64fe',
               'RAILWAY_ENVIRONMENT_ID':'3f436900-ab27-4b41-9248-1a9a9f8dc80c'}
        with patch.dict('os.environ', env), patch.object(gate.db, '_is_pg', True), patch.object(gate.db, 'configured_postgres_unavailable', return_value=False), patch.object(gate.db, 'load_key_value', new_callable=AsyncMock, return_value=None), patch.object(gate.db, 'save_key_value', new_callable=AsyncMock, return_value=True) as write:
            self.assertTrue(await gate.entries_blocked(SimpleNamespace(daily_stopped=False), datetime(2026,9,13,tzinfo=timezone.utc)))
            write.assert_awaited_once()
            write.reset_mock()
            self.assertFalse(await gate.entries_blocked(SimpleNamespace(daily_stopped=False), datetime(2026,9,14,tzinfo=timezone.utc)))
            with patch.dict('os.environ', {'RAILWAY_SERVICE_ID':'other'}):
                self.assertFalse(await gate.entries_blocked(SimpleNamespace(daily_stopped=False), datetime(2026,9,13,tzinfo=timezone.utc)))
            write.assert_not_awaited()

    async def test_restart_and_utc_rollover(self):
        memory = {}
        async def read(key, **kw): return memory.get(key)
        async def write(key, value, **kw): memory[key] = value; return True
        day = datetime(2026, 9, 13, tzinfo=timezone.utc)
        with patch.dict('os.environ', {'RAILWAY_SERVICE_ID': ''}), patch.object(gate.db, 'configured_postgres_unavailable', return_value=False), patch.object(gate.db, 'load_key_value', side_effect=read), patch.object(gate.db, 'save_key_value', side_effect=write):
            first = SimpleNamespace(daily_stopped=True, daily_tracker=SimpleNamespace(daily_stopped=True))
            self.assertTrue(await gate.entries_blocked(first, day))
            restart = SimpleNamespace(daily_stopped=False, daily_tracker=SimpleNamespace(daily_stopped=False))
            self.assertTrue(await gate.entries_blocked(restart, day))
            self.assertTrue(restart.daily_tracker.daily_stopped)
            restart.daily_stopped = restart.daily_tracker.daily_stopped = False
            self.assertFalse(await gate.entries_blocked(restart, datetime(2026, 9, 14, tzinfo=timezone.utc)))

    async def test_storage_failure_blocks_without_fabricating_stop(self):
        e = SimpleNamespace(daily_stopped=False)
        with patch.object(gate.db, 'configured_postgres_unavailable', return_value=True):
            self.assertTrue(await gate.entries_blocked(e))
            self.assertFalse(e.daily_stopped)

    async def test_paper_and_shadow_do_not_access_storage(self):
        with patch.object(gate.db, 'load_key_value', new_callable=AsyncMock) as read:
            for e in (SimpleNamespace(paper_trade=True), SimpleNamespace(_validation_safety_lock_active=True)):
                self.assertFalse(await gate.entries_blocked(e))
            read.assert_not_called()

    def test_key_stable_across_deployments(self):
        with patch.dict('os.environ', {'RAILWAY_DEPLOYMENT_ID':'one'}): a = gate.state_key('2026-09-13')
        with patch.dict('os.environ', {'RAILWAY_DEPLOYMENT_ID':'two'}): self.assertEqual(a, gate.state_key('2026-09-13'))
