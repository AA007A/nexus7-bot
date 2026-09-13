import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from bot import external_origin_runtime as runtime
from bot import external_origin_observation as evidence


class ExternalOriginObservationTests(unittest.IsolatedAsyncioTestCase):
    async def test_record_external_position_persists_read_only_evidence(self):
        row = {
            'symbol': 'LTCUSDT', 'side': 'Buy', 'size': 13,
            'sizeUnit': 'BASE_ASSET', 'entryPrice': 54.8,
        }
        with patch.object(evidence.db, 'load_key_value', new_callable=AsyncMock, return_value=None), \
             patch.object(evidence.db, 'save_key_value', new_callable=AsyncMock, return_value=True) as save, \
             patch.object(evidence.log, 'info'):
            self.assertTrue(await evidence.record_external_position(row, 'TEST_EXTERNAL'))
            payload = save.await_args.args[1]
            self.assertIn('EXTERNAL_READ_ONLY', payload)
            self.assertIn('LIVE_EXTERNAL_POSITION_GUARD', payload)
            self.assertIn('LTCUSDT', payload)

    async def test_matching_observation_requires_symbol_side_and_lifecycle_overlap(self):
        payload = ('{"version":1,"observations":['
                   '{"classification":"EXTERNAL_READ_ONLY","symbol":"LTCUSDT",'
                   '"direction":"LONG","observed_at_ms":1500,"fingerprint":"x"}]}')
        row = {'symbol': 'LTCUSDTM', 'side': 'LONG', 'openTime': 1000, 'closeTime': 2000}
        with patch.object(evidence.db, 'load_key_value', new_callable=AsyncMock, return_value=payload):
            match = await evidence.matching_external_observation(row)
            self.assertIsNotNone(match)
            self.assertEqual(match['fingerprint'], 'x')

    async def test_observation_outside_lifecycle_is_not_used(self):
        payload = ('{"version":1,"observations":['
                   '{"classification":"EXTERNAL_READ_ONLY","symbol":"LTCUSDT",'
                   '"direction":"LONG","observed_at_ms":9999999,"fingerprint":"x"}]}')
        row = {'symbol': 'LTCUSDTM', 'side': 'LONG', 'openTime': 1000, 'closeTime': 2000}
        with patch.object(evidence.db, 'load_key_value', new_callable=AsyncMock, return_value=payload):
            self.assertIsNone(await evidence.matching_external_observation(row))


class ExternalOriginRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_observation_can_classify_external_without_bgx_conflict(self):
        accounting = SimpleNamespace(_opening_fill_order_ids=lambda receipt, row: [])
        original = AsyncMock(return_value=('UNKNOWN_UNATTRIBUTED', 'NO_COMPLETE_OPENING_FILL_IDENTITY'))
        with patch.object(runtime, 'matching_external_observation', new_callable=AsyncMock,
                          return_value={'classification': 'EXTERNAL_READ_ONLY'}):
            origin, reason = await runtime._classify_with_live_observation(
                accounting, original, None, {'ownership': 'UNATTRIBUTED'}, [],
                {'symbol': 'LTCUSDTM', 'side': 'LONG'}
            )
        self.assertEqual(origin, 'MANUAL_EXTERNAL')
        self.assertEqual(reason, 'LIVE_EXTERNAL_READ_ONLY_OBSERVATION')

    async def test_bgx_receipt_conflict_remains_unknown(self):
        accounting = SimpleNamespace(_opening_fill_order_ids=lambda receipt, row: ['bgx-order'])
        original = AsyncMock(return_value=('UNKNOWN_UNATTRIBUTED', 'LINEAGE_MISSING'))
        with patch.object(runtime, 'matching_external_observation', new_callable=AsyncMock,
                          return_value={'classification': 'EXTERNAL_READ_ONLY'}):
            origin, reason = await runtime._classify_with_live_observation(
                accounting, original, None, {'ownership': 'BGX_ORDER_IDS'},
                [{'order_id': 'bgx-order'}], {'symbol': 'LTCUSDTM', 'side': 'LONG'}
            )
        self.assertEqual(origin, 'UNKNOWN_UNATTRIBUTED')
        self.assertEqual(reason, 'LIVE_EXTERNAL_OBSERVATION_CONFLICTS_WITH_BGX_ORDER_HISTORY')

    async def test_existing_bgx_confirmed_result_is_never_overridden(self):
        accounting = SimpleNamespace(_opening_fill_order_ids=lambda receipt, row: [])
        original = AsyncMock(return_value=('BGX_CONFIRMED', 'DURABLE_IDS_FILLS_AND_LINEAGE'))
        with patch.object(runtime, 'matching_external_observation', new_callable=AsyncMock) as lookup:
            origin, reason = await runtime._classify_with_live_observation(
                accounting, original, None, {}, [], {'symbol': 'BTCUSDTM', 'side': 'LONG'}
            )
        self.assertEqual(origin, 'BGX_CONFIRMED')
        lookup.assert_not_awaited()

    async def test_candidate_requires_settle_delay_before_persistence(self):
        engine = SimpleNamespace(
            paper_trade=False,
            positions={},
            _external_position_symbols={'ADAUSDT'},
            client=SimpleNamespace(get_positions=AsyncMock(return_value=[
                {'symbol': 'ADAUSDT', 'side': 'Buy', 'size': 10, 'entryPrice': 1.0}
            ])),
        )
        log = Mock()
        proof = SimpleNamespace(recovered=False, reason='NO_DURABLE_ORDER')
        with patch.object(runtime, 'prove_restart_ownership', new_callable=AsyncMock, return_value=proof), \
             patch.object(runtime, 'record_external_position', new_callable=AsyncMock, return_value=True) as record:
            await runtime._observe_external_candidates(engine, log)
            record.assert_not_awaited()
            engine._external_origin_pending[('ADAUSDT', 'BUY')] -= runtime._SETTLE_S + 1.0
            await runtime._observe_external_candidates(engine, log)
            record.assert_awaited_once()

    async def test_exact_restart_ownership_proof_prevents_external_record(self):
        engine = SimpleNamespace(
            paper_trade=False,
            positions={},
            _external_position_symbols=set(),
            client=SimpleNamespace(get_positions=AsyncMock(return_value=[
                {'symbol': 'BTCUSDT', 'side': 'Buy', 'size': 1, 'entryPrice': 60000}
            ])),
            _external_origin_pending={
                ('BTCUSDT', 'BUY'): runtime.time.monotonic() - runtime._SETTLE_S - 1.0
            },
        )
        proof = SimpleNamespace(recovered=True, reason='EXACT_PROOF')
        with patch.object(runtime, 'prove_restart_ownership', new_callable=AsyncMock, return_value=proof), \
             patch.object(runtime, 'record_external_position', new_callable=AsyncMock) as record:
            await runtime._observe_external_candidates(engine, Mock())
        record.assert_not_awaited()
        self.assertNotIn(('BTCUSDT', 'BUY'), engine._external_origin_pending)

    async def test_install_is_idempotent(self):
        calls = {'guard': 0}

        class Engine:
            async def _guard_naked_positions(self):
                calls['guard'] += 1
                return 'ok'

        accounting = SimpleNamespace(_classify_origin=AsyncMock(return_value=('UNKNOWN_UNATTRIBUTED', 'X')),
                                     _opening_fill_order_ids=lambda receipt, row: [])
        runtime.install(Engine, accounting, Mock())
        first = Engine._guard_naked_positions
        runtime.install(Engine, accounting, Mock())
        self.assertIs(Engine._guard_naked_positions, first)
        engine = Engine()
        engine.paper_trade = True
        self.assertEqual(await engine._guard_naked_positions(), 'ok')
        self.assertEqual(calls['guard'], 1)


if __name__ == '__main__':
    unittest.main()
