import json
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot import durable_daily_stop as gate


class Stats:
    def __init__(self, day, realized):
        self.trades = []
        self._durable_daily_pnl = {
            day: {
                'a' * 64: {
                    'pnl': realized,
                    'source': 'TEST',
                    'closed_at': f'{day}T12:00:00+00:00',
                }
            }
        }


class DailyStopOperatorOverrideTests(unittest.IsolatedAsyncioTestCase):
    def engine(self, day='2026-09-14', realized=-3.05, limit=0.86, stopped=True):
        return SimpleNamespace(
            daily_stopped=stopped,
            daily_tracker=SimpleNamespace(daily_stopped=stopped, daily_stop_loss=limit),
            daily_stop_loss=limit,
            _daily_pnl_ok=True,
            stats=Stats(day, realized),
            positions={},
        )

    async def test_valid_stored_stop_is_bypassed_only_on_exact_utc_day(self):
        day = '2026-09-14'
        state = json.dumps({
            'version': 3,
            'day': day,
            'trigger_pnl': -0.6435,
            'stop_limit': 0.41,
            'triggered_at': f'{day}T00:30:00+00:00',
            'source': 'RUNTIME_DAILY_STOP',
        })
        engine = self.engine(day=day, stopped=False)
        env = {'RAILWAY_SERVICE_ID': '', 'DAILY_STOP_OVERRIDE_UTC_DAY': day}
        with patch.dict('os.environ', env, clear=False), \
             patch.object(gate.db, 'configured_postgres_unavailable', return_value=False), \
             patch.object(gate.db, 'load_key_value', new_callable=AsyncMock, return_value=state), \
             patch.object(gate.db, 'save_key_value', new_callable=AsyncMock) as write:
            blocked = await gate.entries_blocked(
                engine, datetime(2026, 9, 14, 14, 30, tzinfo=timezone.utc)
            )
            self.assertFalse(blocked)
            self.assertFalse(engine.daily_stopped)
            self.assertFalse(engine.daily_tracker.daily_stopped)
            write.assert_not_awaited()

    async def test_override_auto_expires_on_next_utc_day(self):
        state = json.dumps({
            'version': 3,
            'day': '2026-09-15',
            'trigger_pnl': -1.2,
            'stop_limit': 1.0,
            'triggered_at': '2026-09-15T00:01:00+00:00',
            'source': 'RUNTIME_DAILY_STOP',
        })
        engine = self.engine(day='2026-09-15', realized=-1.2, limit=1.0, stopped=False)
        env = {'RAILWAY_SERVICE_ID': '', 'DAILY_STOP_OVERRIDE_UTC_DAY': '2026-09-14'}
        with patch.dict('os.environ', env, clear=False), \
             patch.object(gate.db, 'configured_postgres_unavailable', return_value=False), \
             patch.object(gate.db, 'load_key_value', new_callable=AsyncMock, return_value=state):
            blocked = await gate.entries_blocked(
                engine, datetime(2026, 9, 15, 0, 5, tzinfo=timezone.utc)
            )
            self.assertTrue(blocked)
            self.assertTrue(engine.daily_stopped)
            self.assertTrue(engine.daily_tracker.daily_stopped)

    async def test_fresh_breach_is_persisted_before_override_releases_entries(self):
        day = '2026-09-14'
        engine = self.engine(day=day, realized=-3.05, limit=0.86, stopped=True)
        env = {'RAILWAY_SERVICE_ID': '', 'DAILY_STOP_OVERRIDE_UTC_DAY': day}
        with patch.dict('os.environ', env, clear=False), \
             patch.object(gate.db, 'configured_postgres_unavailable', return_value=False), \
             patch.object(gate.db, 'load_key_value', new_callable=AsyncMock, return_value=None), \
             patch.object(gate.db, 'save_key_value', new_callable=AsyncMock, return_value=True) as write:
            blocked = await gate.entries_blocked(
                engine, datetime(2026, 9, 14, 14, 30, tzinfo=timezone.utc)
            )
            self.assertFalse(blocked)
            write.assert_awaited_once()
            persisted = json.loads(write.await_args.args[1])
            self.assertLessEqual(persisted['trigger_pnl'], -persisted['stop_limit'])
            self.assertFalse(engine.daily_stopped)
            self.assertFalse(engine.daily_tracker.daily_stopped)

    async def test_storage_failure_remains_fail_closed_even_with_override(self):
        day = '2026-09-14'
        engine = self.engine(day=day)
        with patch.dict('os.environ', {'DAILY_STOP_OVERRIDE_UTC_DAY': day}, clear=False), \
             patch.object(gate.db, 'configured_postgres_unavailable', return_value=True):
            blocked = await gate.entries_blocked(
                engine, datetime(2026, 9, 14, 14, 30, tzinfo=timezone.utc)
            )
            self.assertTrue(blocked)


if __name__ == '__main__':
    unittest.main()
