import copy
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.accounting_fill_link import fills, reconcile, recent_fills


class FillLinkTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.row = dict(closeId='p1', symbol='ETHUSDTM', side='LONG',
                        settleCurrency='USDT', openTime=10000, closeTime=20000,
                        openPrice='100', closePrice='90', tradeFee='0.12')
        self.orders = [dict(order_id='open', client_oid='bgx7-test',
                            state='FILLED', symbol='ETHUSDT', side='Buy')]
        self.executions = [dict(tradeId='1', orderId='open', symbol='ETHUSDTM',
            side='buy', size=2, price='100', fee='0.06', feeCurrency='USDT',
            tradeTime=10000000000, tradeType='trade'),
            dict(tradeId='2', orderId='native-stop', symbol='ETHUSDTM',
            side='sell', size=2, price='90', fee='0.06', feeCurrency='USDT',
            tradeTime=20000000000, tradeType='trade')]

    def client(self, rows=None):
        items = self.executions if rows is None else rows
        return SimpleNamespace(_get=AsyncMock(return_value=dict(
            currentPage=1, totalPage=1, totalNum=len(items), items=items)))

    async def test_exact_ids_balance_prices_fees_confirm(self):
        client = self.client()
        result = await reconcile(client, self.row, [self.row], self.orders)
        self.assertTrue(result['fills_reconciled'])
        self.assertEqual(result['opening_order_ids'], ['open'])
        self.assertEqual(result['closing_order_ids'], ['native-stop'])
        self.assertEqual(result['contracts'], '2')
        self.assertEqual(client._get.call_args.args[0], '/api/v1/fills')

    async def test_recent_fills_fast_path_requires_complete_exchange_accounting(self):
        now_ms = 1_800_000_000_000
        row = dict(self.row, openTime=now_ms-10000, closeTime=now_ms,
                   openPrice='100', closePrice='90', tradeFee='0.12')
        recent = copy.deepcopy(self.executions)
        recent[0]['tradeTime'] = (now_ms-10000) * 1000000
        recent[1]['tradeTime'] = now_ms * 1000000
        client = SimpleNamespace(_get=AsyncMock(return_value=recent))
        with patch('time.time', return_value=now_ms/1000):
            result = await recent_fills(client, row)
        self.assertEqual(len(result), 2)
        self.assertEqual(client._get.call_args.args[0], '/api/v1/recentFills')

    async def test_incomplete_recent_fills_falls_back_to_paginated_history(self):
        now_ms = 1_800_000_000_000
        row = dict(self.row, openTime=now_ms-10000, closeTime=now_ms)
        partial = copy.deepcopy(self.executions[:1])
        partial[0]['tradeTime'] = (now_ms-10000) * 1000000
        history = dict(currentPage=1, totalPage=1, totalNum=2, items=self.executions)
        client = SimpleNamespace(_get=AsyncMock(side_effect=[partial, history]))
        with patch('time.time', return_value=now_ms/1000):
            result = await fills(client, row)
        self.assertEqual(len(result), 2)
        self.assertEqual(client._get.call_args.args[0], '/api/v1/fills')

    async def test_external_increase_never_attributed(self):
        self.executions[0]['orderId'] = 'external'
        result = await reconcile(self.client(), self.row, [self.row], self.orders)
        self.assertFalse(result['fills_reconciled'])
        self.assertEqual(result['reconciliation_reason'], 'EXTERNAL_OR_UNKNOWN_OPENING_ORDER')

    async def test_missing_malformed_or_inconsistent_data_is_pending(self):
        for field, value in [('size', 1), ('price', 'NaN'), ('fee', '0.07'),
                             ('feeCurrency', 'BTC'), ('tradeType', 'adl'),
                             ('tradeTime', 90000000000)]:
            with self.subTest(field=field):
                rows = copy.deepcopy(self.executions)
                rows[1][field] = value
                result = await reconcile(self.client(rows), self.row, [self.row], self.orders)
                self.assertFalse(result['fills_reconciled'])
        del self.executions[1]['fee']
        result = await reconcile(self.client(), self.row, [self.row], self.orders)
        self.assertFalse(result['fills_reconciled'])

    async def test_shared_position_boundary_is_ambiguous(self):
        other = dict(self.row, closeId='p2', openTime=20000, closeTime=30000)
        client = self.client()
        result = await reconcile(client, self.row, [self.row, other], self.orders)
        self.assertEqual(result['reconciliation_reason'], 'OVERLAPPING_POSITION_HISTORY')
        client._get.assert_not_called()

    async def test_missing_registry_does_not_query_or_infer_ownership(self):
        client = self.client()
        result = await reconcile(client, self.row, [self.row], [])
        self.assertEqual(result['ownership'], 'UNATTRIBUTED')
        client._get.assert_not_called()

    async def test_paginated_partial_fills(self):
        c = SimpleNamespace(_get=AsyncMock(side_effect=[
            dict(currentPage=1, totalPage=2, totalNum=2, items=self.executions[:1]),
            dict(currentPage=2, totalPage=2, totalNum=2, items=self.executions[1:])]))
        self.assertTrue((await reconcile(c, self.row, [self.row], self.orders))['fills_reconciled'])
        self.assertEqual(c._get.call_args.kwargs['params']['currentPage'], 2)

    async def test_truncated_page_cannot_confirm(self):
        c = self.client()
        c._get.return_value['totalNum'] = 3
        with self.assertRaises(ValueError):
            await fills(c, self.row)
