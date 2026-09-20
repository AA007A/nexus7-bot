import copy
import json
import unittest
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.durable_daily_pnl import checkpoint, realized
from bot.durable_execution import can_open
from bot import daily_pnl_storage


class DailyPnlTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 13, 12, tzinfo=timezone.utc)
        self.store = {}
        async def load(key, **kw):
            return self.store.get(key)
        async def save(key, value, **kw):
            self.store[key] = value
            return True
        for target, value in [('load_key_value', AsyncMock(side_effect=load)),
                              ('save_key_value', AsyncMock(side_effect=save)),
                              ('configured_postgres_unavailable', lambda: False),
                              ('_is_pg', True)]:
            p = patch('bot.durable_daily_pnl.db.' + target, value)
            p.start()
            self.addCleanup(p.stop)

    def engine(self):
        return SimpleNamespace(paper_trade=False, _durable_state_enforced=True,
            _durable_state_ok=True, stats=SimpleNamespace(trades=[]))

    def trade(self, pnl=-0.1):
        return SimpleNamespace(symbol='ETHUSDT', direction='LONG', qty=1, entry=100,
            exit_price=99, opened_at=self.now - timedelta(hours=1),
            closed_at=self.now, pnl=pnl)

    async def test_restart_restores_loss_below_stop_without_double_counting(self):
        a = self.engine()
        t = self.trade()
        self.assertTrue(await checkpoint(a, extra=t, now=self.now))
        a.stats.trades.append(t)
        self.assertTrue(await checkpoint(a, now=self.now))
        b = self.engine()
        self.assertTrue(await checkpoint(b, now=self.now))
        self.assertAlmostEqual(realized(b.stats, self.now), -0.1)
        b.stats.trades.append(copy.deepcopy(t))
        self.assertTrue(await checkpoint(b, now=self.now))
        self.assertAlmostEqual(realized(b.stats, self.now), -0.1)
        newer = self.trade(-0.2)
        newer.closed_at += timedelta(seconds=1)
        b.stats.trades.append(newer)
        await checkpoint(b, now=self.now)
        self.assertAlmostEqual(realized(b.stats, self.now), -0.3)

    async def test_new_utc_day_does_not_inherit_yesterday(self):
        e = self.engine()
        e.stats.trades.append(self.trade())
        await checkpoint(e, now=self.now)
        tomorrow = self.now + timedelta(days=1)
        await checkpoint(e, now=tomorrow)
        self.assertEqual(realized(e.stats, tomorrow), 0)

    async def test_write_failure_blocks_dispatch_and_retry_is_idempotent(self):
        e = self.engine()
        e.stats.trades.append(self.trade())
        with patch('bot.durable_daily_pnl.db.save_key_value', AsyncMock(return_value=False)):
            self.assertFalse(await checkpoint(e, now=self.now))
        self.assertFalse(can_open(e))
        self.assertTrue(await checkpoint(e, now=self.now))
        self.assertTrue(can_open(e))
        self.assertAlmostEqual(realized(e.stats, self.now), -0.1)

    async def test_corrupt_storage_is_not_zero(self):
        e = self.engine()
        with patch('bot.durable_daily_pnl.db.load_key_value', AsyncMock(return_value='{}')):
            self.assertFalse(await checkpoint(e, now=self.now))
        self.assertFalse(can_open(e))

    async def test_nan_rejected_and_net_fees_not_subtracted_again(self):
        e = self.engine()
        self.assertFalse(await checkpoint(e, extra=self.trade(float('nan')), now=self.now))
        t = self.trade(-0.13)
        t.total_fees = .03
        t.accounting_source = 'ESTIMATED_LOCAL_MARK_AND_FEE_RATE'
        await checkpoint(e, extra=t, now=self.now)
        self.assertAlmostEqual(realized(e.stats, self.now), -.13)
        self.assertIn(t.accounting_source, next(iter(self.store.values())))

    async def test_paper_shadow_are_unchanged(self):
        for attr in ('paper_trade', '_validation_safety_lock_active'):
            e = self.engine()
            setattr(e, attr, True)
            self.assertTrue(await checkpoint(e, now=self.now))
        self.assertFalse(self.store)

    async def test_conflicting_replay_blocks(self):
        e = self.engine()
        t = self.trade()
        await checkpoint(e, extra=t, now=self.now)
        t.pnl = -.2
        self.assertFalse(await checkpoint(e, extra=t, now=self.now))
        self.assertFalse(can_open(e))

    async def test_legacy_versions_merge_without_reset_or_double_count(self):
        e = self.engine()
        await checkpoint(e, extra=self.trade(-10), now=self.now)
        day = self.now.date().isoformat()
        canonical = daily_pnl_storage.ledger_key(day)
        old = self.store.pop(canonical)
        for key in daily_pnl_storage.legacy_keys(day):
            self.store[key] = old
        restarted = self.engine()
        with patch('bot.durable_daily_stop.STATE_VERSION', 99):
            self.assertTrue(await checkpoint(restarted, now=self.now))
        self.assertEqual(realized(restarted.stats, self.now), -10)
        self.assertIn(canonical, self.store)
        for key in daily_pnl_storage.legacy_keys(day):
            self.assertEqual(self.store[key], old)
        self.assertTrue(await checkpoint(restarted, now=self.now))
        self.assertEqual(realized(restarted.stats, self.now), -10)

    async def test_legacy_conflict_blocks_without_rewriting_any_evidence(self):
        e = self.engine()
        await checkpoint(e, extra=self.trade(-10), now=self.now)
        day = self.now.date().isoformat()
        old = json.loads(self.store[daily_pnl_storage.ledger_key(day)])
        next(iter(old['events'].values()))['pnl'] = -20
        self.store[daily_pnl_storage.legacy_keys(day)[1]] = json.dumps(old)
        before = copy.deepcopy(self.store)
        self.assertFalse(await checkpoint(self.engine(), now=self.now))
        self.assertEqual(self.store, before)

    async def test_late_legacy_writer_imported_on_next_checkpoint(self):
        from bot.durable_daily_pnl import event
        e = self.engine()
        await checkpoint(e, extra=self.trade(-10), now=self.now)
        day = self.now.date().isoformat()
        token, value = event(self.trade(-20))
        # Change the event identity to model a distinct completed trade.
        later = self.trade(-20)
        later.closed_at += timedelta(seconds=1)
        token, value = event(later)
        self.store[daily_pnl_storage.legacy_keys(day)[2]] = json.dumps(
            {'version': 1, 'day': day, 'events': {token: value}})
        self.assertTrue(await checkpoint(e, now=self.now))
        self.assertEqual(realized(e.stats, self.now), -30)
