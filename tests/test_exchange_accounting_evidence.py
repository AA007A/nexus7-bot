import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from bot.exchange_accounting_evidence import collect, schedule, audit


class AccountingTests(unittest.IsolatedAsyncioTestCase):
    async def test_pagination_preserves_missing_and_zero(self):
        client = SimpleNamespace(_get=AsyncMock(side_effect=[
            dict(currentPage=1,totalPage=2,items=[dict(closeId='1',closeTime=2,pnl='0',userId='private')]),
            dict(currentPage=2,totalPage=2,items=[dict(closeId='2',closeTime=3,fundingFee='0')])]))
        rows = await collect(client, 1, 4)
        self.assertEqual(len(rows), 2)
        self.assertNotIn('fundingFee', rows[0])
        self.assertNotIn('userId', rows[0])
        self.assertEqual(rows[1]['fundingFee'], '0')
        self.assertEqual(client._get.call_args.kwargs['params']['pageId'], 2)

    async def test_missing_page_is_not_zero(self):
        c=SimpleNamespace(_get=AsyncMock(return_value={}))
        with self.assertRaises(ValueError): await collect(c, 1, 4)

    async def test_outside_window_is_rejected(self):
        c=SimpleNamespace(_get=AsyncMock(return_value=dict(currentPage=1,totalPage=1,items=[dict(closeId='1',closeTime=8)])))
        with self.assertRaises(ValueError): await collect(c, 1, 4)

    async def test_paper_and_shadow_never_schedule(self):
        for e in (SimpleNamespace(paper_trade=True), SimpleNamespace(_validation_safety_lock_active=True)):
            schedule(e)
            self.assertFalse(hasattr(e, '_accounting_evidence_task'))

    async def test_failed_persistence_never_logs_durable_receipt(self):
        with patch('bot.exchange_accounting_evidence.collect', new_callable=AsyncMock, return_value=[dict(closeId='1')]), patch('bot.exchange_accounting_evidence.db.load_key_value', new_callable=AsyncMock, return_value=None), patch('bot.exchange_accounting_evidence.db.save_key_value', new_callable=AsyncMock, return_value=False), patch('bot.exchange_accounting_evidence.log') as log:
            await audit(SimpleNamespace(client=None))
            self.assertFalse(log.info.called)
            self.assertTrue(log.warning.called)
