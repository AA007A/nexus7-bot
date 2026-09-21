import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from tests.test_ai_gate import EngineFixture, E


class EntryPauseTests(EngineFixture):
    async def test_paused_entry_blocked_but_native_reduction_allowed(self):
        self.engine.active = True
        self.engine._running = True
        from bot.kucoin_position_units import KuCoinPositionUnitAdapter
        self.engine.client = KuCoinPositionUnitAdapter(self.client)
        self.engine.pause_entries()
        self.assertTrue(self.engine.active)
        self.assertTrue(self.engine._running)
        session = Mock()
        self.client._session = session
        for endpoint in ('/api/v1/orders', '/api/v1/st-orders'):
            with self.assertRaises(ValueError):
                self.client._entry_safe_post(endpoint, {}, 'unused')
        session.post.assert_not_called()
        self.client._entry_safe_post('/api/v1/orders', {'reduceOnly': True}, 'unused')
        session.post.assert_called_once()
        self.engine.resume_entries()
        self.assertFalse(self.client.entries_paused)

    async def test_paused_loop_manages_position_and_cleans_workers_on_exit(self):
        e = self.engine
        e.connected = e.active = True
        e._running = False
        e.positions = {'TESTUSDT': SimpleNamespace(pnl=0)}
        e.pause_entries()
        e.active = False
        managed = ('_guard_naked_positions', '_sync_positions',
                   '_check_stagnation_and_invalidation', '_manage_partial_tp',
                   '_apply_trailing_stops', '_check_rr_double')
        for name in managed + ('_connect', '_update_balance', '_heartbeat_telegram'):
            setattr(e, name, AsyncMock())
        e._check_daily_reset = Mock()
        e._gc_caches = Mock()
        e._update_daily_pnl = Mock()
        e._scan_all_and_enter = AsyncMock()
        async def worker(*args):
            await asyncio.Future()
        tasks = []
        original_start = e._start_background
        def start(coro):
            t = original_start(coro)
            tasks.append(t)
            return t
        e._start_background = start
        e._monitor_news_pipeline = worker
        real_sleep = asyncio.sleep
        async def finish_cycle(seconds):
            await real_sleep(0)
            e._running = False
        with patch.object(E.db, 'init', AsyncMock()), \
             patch.object(E.durable, 'restore_engine_state', AsyncMock()), \
             patch.object(E.durable, 'reconcile_orders', AsyncMock()), \
             patch('bot.initial_reconciliation.finalize_initial_reconciliation', AsyncMock(return_value=False)), \
             patch('bot.protection_readiness.refresh_protection_readiness', AsyncMock(return_value=False)), \
             patch('bot.execution_ownership.initialize_live_execution_ownership', AsyncMock()), \
             patch('bot.execution_ownership.execution_ownership_heartbeat', worker), \
             patch.object(E.scoring, 'update_macro_cache', worker), \
             patch.object(E.scoring, 'news_reader_loop', worker), \
             patch.object(E.mdata, 'update_macro_correlations', worker), \
             patch.object(E.bt, 'weekly_backtest_loop', worker), \
             patch.object(E.opt, 'weekly_optimization_loop', worker), \
             patch('bot.durable_daily_pnl.checkpoint', AsyncMock(return_value=True)), \
             patch('bot.durable_daily_stop.entries_blocked', AsyncMock(return_value=False)), \
             patch('bot.exchange_accounting_evidence.schedule'), \
             patch.object(E.asyncio, 'sleep', finish_cycle):
            await e.run()
            for name in managed:
                getattr(e, name).assert_awaited_once()
            e._scan_all_and_enter.assert_not_awaited()
            self.assertEqual(len(tasks), 7)
            self.assertTrue(all(t.done() for t in tasks))
            self.assertFalse(e._background_tasks)
            await e.run()
            self.assertEqual(len(tasks), 14)
            self.assertTrue(all(t.done() for t in tasks))
