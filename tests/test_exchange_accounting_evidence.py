import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from bot.exchange_accounting_evidence import (
    collect, schedule, audit, collect_ledger, audit_ledger, cashflow_drawdown_shadow, ledger_windows, ledger_reconciliation_summary, _classify_origin, _opening_fill_order_ids,
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


class LedgerAccountingTests(unittest.IsolatedAsyncioTestCase):
    async def test_ledger_paginates_by_offset_and_preserves_zero_fee(self):
        client = SimpleNamespace(_get=AsyncMock(side_effect=[
            {'dataList': [{'offset': 20, 'time': 2, 'type': 'RealisedPNL', 'amount': '-1.2', 'fee': '0'}], 'hasMore': True},
            {'dataList': [{'offset': 10, 'time': 3, 'type': 'TransferIn', 'amount': '5'}], 'hasMore': False},
        ]))
        rows = await collect_ledger(client, 1, 4)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['fee'], '0')
        self.assertEqual(client._get.await_args_list[1].kwargs['params']['offset'], 20)
        for call in client._get.await_args_list:
            self.assertEqual(call.args[0], '/api/v1/transaction-history')
            self.assertTrue(call.kwargs['auth'])

    async def test_ledger_rejects_window_over_24_hours(self):
        client = SimpleNamespace(_get=AsyncMock())
        with self.assertRaises(ValueError):
            await collect_ledger(client, 1, 86400002)
        client._get.assert_not_awaited()

    async def test_ledger_rejects_rows_outside_window(self):
        client = SimpleNamespace(_get=AsyncMock(return_value={
            'dataList': [{'offset': 1, 'time': 9}], 'hasMore': False,
        }))
        with self.assertRaises(ValueError):
            await collect_ledger(client, 1, 4)

    async def test_ledger_stalled_pagination_fails_closed(self):
        client = SimpleNamespace(_get=AsyncMock(side_effect=[
            {'dataList': [{'offset': 20, 'time': 2}], 'hasMore': True},
            {'dataList': [{'offset': 20, 'time': 2}], 'hasMore': True},
        ]))
        with self.assertRaises(ValueError):
            await collect_ledger(client, 1, 4)

    async def test_ledger_failed_persistence_never_logs_durable_receipt(self):
        with patch('bot.exchange_accounting_evidence.collect_ledger', new_callable=AsyncMock, return_value=[{'offset': '1', 'time': 2}]), \
             patch('bot.exchange_accounting_evidence.db.load_key_value', new_callable=AsyncMock, return_value=None), \
             patch('bot.exchange_accounting_evidence.db.save_key_value', new_callable=AsyncMock, return_value=False), \
             patch('bot.exchange_accounting_evidence.log') as log:
            await audit_ledger(SimpleNamespace(client=None))
            self.assertFalse(log.info.called)
            self.assertTrue(log.warning.called)
            self.assertIn('complete=false', log.warning.call_args.args[0])

    def test_extended_ledger_windows_are_daily_contiguous_and_bounded(self):
        windows = ledger_windows(14 * 86400000, 14)
        self.assertEqual(len(windows), 14)
        self.assertEqual(windows[0], (0, 86400000))
        self.assertEqual(windows[-1], (13 * 86400000, 14 * 86400000))
        for left, right in windows:
            self.assertGreater(right, left)
            self.assertLessEqual(right - left, 86400000)
        with self.assertRaises(ValueError):
            ledger_windows(86400000, 91)

    def test_reconciliation_summary_classifies_types_without_netting_unknowns(self):
        rows = [
            {'offset': 1, 'type': 'TransferIn', 'amount': '38.1', 'fee': '0'},
            {'offset': 2, 'type': 'RealisedPNL', 'amount': '-9', 'fee': '0.2'},
            {'offset': 3, 'type': 'FundingFee', 'amount': '-1.5', 'fee': '0'},
        ]
        out = ledger_reconciliation_summary(rows)
        self.assertEqual(out['rows'], 3)
        self.assertAlmostEqual(out['by_type']['TransferIn']['amount'], 38.1)
        self.assertAlmostEqual(out['by_type']['RealisedPNL']['amount'], -9.0)
        self.assertAlmostEqual(out['by_type']['FundingFee']['amount'], -1.5)
        self.assertAlmostEqual(out['fee_observed'], 0.2)


class CashflowDrawdownShadowTests(unittest.TestCase):
    def test_deposit_before_peak_does_not_rebase_existing_peak(self):
        out = cashflow_drawdown_shadow([
            {'offset': 1, 'time': 1000, 'type': 'TransferIn', 'amount': '38.16805178', 'fee': '0'},
            {'offset': 2, 'time': 2000, 'type': 'RealisedPNL', 'amount': '-9', 'fee': '0'},
        ], 20.6452, 44.6737, peak_recorded_ms=3000)
        self.assertAlmostEqual(out['shadow_peak'], 44.6737)
        self.assertAlmostEqual(out['post_peak_external_net'], 0.0)
        self.assertAlmostEqual(out['external_net'], 38.16805178)
        self.assertAlmostEqual(out['realised_pnl'], -9.0)

    def test_deposit_after_peak_adds_to_shadow_peak(self):
        out = cashflow_drawdown_shadow([
            {'offset': 1, 'time': 4000, 'type': 'TransferIn', 'amount': '10', 'fee': '0'},
        ], 40, 50, peak_recorded_ms=3000)
        self.assertAlmostEqual(out['shadow_peak'], 60.0)
        self.assertAlmostEqual(out['shadow_drawdown'], 1/3)

    def test_withdrawal_after_peak_reduces_shadow_peak_but_not_below_equity(self):
        out = cashflow_drawdown_shadow([
            {'offset': 1, 'time': 4000, 'type': 'TransferOut', 'amount': '30', 'fee': '0'},
        ], 25, 50, peak_recorded_ms=3000)
        self.assertAlmostEqual(out['shadow_peak'], 25.0)
        self.assertAlmostEqual(out['shadow_drawdown'], 0.0)

    def test_realised_pnl_never_rebases_peak(self):
        out = cashflow_drawdown_shadow([
            {'offset': 1, 'time': 4000, 'type': 'RealisedPNL', 'amount': '-20', 'fee': '1.25'},
        ], 30, 50, peak_recorded_ms=3000)
        self.assertAlmostEqual(out['shadow_peak'], 50.0)
        self.assertAlmostEqual(out['realised_pnl'], -20.0)
        self.assertAlmostEqual(out['fee_observed_not_applied'], 1.25)

    def test_duplicate_offset_is_deduped_and_conflict_fails_closed(self):
        row = {'offset': 1, 'time': 1000, 'type': 'TransferIn', 'amount': '10'}
        out = cashflow_drawdown_shadow([row, dict(row)], 20, 20, peak_recorded_ms=2000)
        self.assertEqual(out['rows'], 1)
        bad = dict(row, amount='11')
        with self.assertRaises(ValueError):
            cashflow_drawdown_shadow([row, bad], 20, 20, peak_recorded_ms=2000)
