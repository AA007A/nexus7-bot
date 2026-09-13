import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from bot.exchange_accounting_evidence import (
    collect, schedule, audit, _classify_origin, _opening_fill_order_ids,
)


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

    def test_opening_fill_order_ids_are_directional_and_deduped(self):
        receipt = {'fills': [
            {'side': 'buy', 'orderId': 'open-1'},
            {'side': 'buy', 'orderId': 'open-1'},
            {'side': 'sell', 'orderId': 'close-1'},
        ]}
        self.assertEqual(_opening_fill_order_ids(receipt, {'side': 'LONG'}), ['open-1'])

    async def test_bgx_requires_ids_fills_and_lineage(self):
        receipt = {
            'ownership': 'BGX_ORDER_IDS', 'fills_reconciled': True,
            'lineage_reconciled': True,
        }
        origin, reason = await _classify_origin(None, receipt, [], {'side': 'LONG'})
        self.assertEqual(origin, 'BGX_CONFIRMED')
        self.assertEqual(reason, 'DURABLE_IDS_FILLS_AND_LINEAGE')

    async def test_non_bgx_client_oid_is_external(self):
        client = SimpleNamespace(_get=AsyncMock(return_value={
            'id': 'manual-1', 'symbol': 'LTCUSDTM', 'side': 'buy',
            'clientOid': 'kucoin-app-generated-oid',
        }))
        receipt = {
            'ownership': 'UNATTRIBUTED', 'fills_reconciled': False,
            'fills': [{'side': 'buy', 'orderId': 'manual-1'}],
        }
        origin, reason = await _classify_origin(
            client, receipt, [], {'side': 'LONG', 'symbol': 'LTCUSDTM'}
        )
        self.assertEqual(origin, 'MANUAL_EXTERNAL')
        self.assertEqual(reason, 'NON_BGX_CLIENT_OID_CONFIRMED')
        client._get.assert_awaited_once_with('/api/v1/orders/manual-1', auth=True)

    async def test_bgx_client_oid_without_registry_stays_unknown(self):
        client = SimpleNamespace(_get=AsyncMock(return_value={
            'id': 'lost-1', 'symbol': 'LTCUSDTM', 'side': 'buy',
            'clientOid': 'bgx7-lost-registry',
        }))
        receipt = {'fills': [{'side': 'buy', 'orderId': 'lost-1'}]}
        origin, reason = await _classify_origin(
            client, receipt, [], {'side': 'LONG', 'symbol': 'LTCUSDTM'}
        )
        self.assertEqual(origin, 'UNKNOWN_UNATTRIBUTED')
        self.assertEqual(reason, 'BGX_CLIENT_OID_WITHOUT_DURABLE_PROOF')

    async def test_durable_order_without_full_proof_stays_unknown_without_lookup(self):
        client = SimpleNamespace(_get=AsyncMock())
        receipt = {'fills': [{'side': 'buy', 'orderId': 'known-1'}]}
        registry = [{'order_id': 'known-1', 'client_oid': 'bgx7-known'}]
        origin, reason = await _classify_origin(
            client, receipt, registry, {'side': 'LONG', 'symbol': 'LTCUSDTM'}
        )
        self.assertEqual(origin, 'UNKNOWN_UNATTRIBUTED')
        self.assertEqual(reason, 'DURABLE_ORDER_PRESENT_WITHOUT_FULL_BGX_PROOF')
        client._get.assert_not_awaited()

    async def test_lookup_failure_stays_unknown(self):
        client = SimpleNamespace(_get=AsyncMock(side_effect=RuntimeError('network')))
        receipt = {'fills': [{'side': 'buy', 'orderId': 'x'}]}
        origin, reason = await _classify_origin(
            client, receipt, [], {'side': 'LONG', 'symbol': 'LTCUSDTM'}
        )
        self.assertEqual(origin, 'UNKNOWN_UNATTRIBUTED')
        self.assertEqual(reason, 'ORDER_IDENTITY_LOOKUP_UNCONFIRMED')
