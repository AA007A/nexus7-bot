import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.post_trade_forensics import (
    _decision_snapshot,
    _lineage,
    _opening_order_for_position,
)
from bot.exchange_accounting_evidence import _load_lineage_for_opening_orders


class PostTradeLineageV2Tests(unittest.IsolatedAsyncioTestCase):
    def test_final_nexus_object_normalized_without_mutation(self):
        decision = SimpleNamespace(
            execution_allowed=True, setup_quality=84.5, confidence=0.91
        )
        snapshot = _decision_snapshot(decision)
        self.assertEqual(snapshot['decision'], 'APPROVE')
        self.assertIs(snapshot['execution_allowed'], True)
        self.assertEqual(snapshot['setup_quality'], 84.5)
        self.assertEqual(snapshot['confidence'], 0.91)
        self.assertTrue(decision.execution_allowed)

    def test_lineage_contains_durable_opening_order_identity(self):
        now = time.time()
        sig = SimpleNamespace(
            symbol='LTCUSDT', direction='LONG', entry=54.37, score=73,
            regime='RANGE', entry_type='PULLBACK'
        )
        order = {
            'order_id': '488650607045902336',
            'client_oid': 'bgx7-example',
            'created_at': now,
        }
        value = _lineage(sig, {'decision': 'APPROVE'}, order)
        self.assertEqual(value['version'], 2)
        self.assertEqual(value['order_id'], '488650607045902336')
        self.assertEqual(value['client_oid'], 'bgx7-example')
        self.assertEqual(value['nexus'], 'APPROVE')
        self.assertGreater(value['captured_at_ms'], 0)

    def test_opening_order_selection_rejects_stale_same_symbol_trade(self):
        now = time.time()
        rows = [
            {'symbol': 'LTCUSDT', 'state': 'FILLED', 'order_id': 'old',
             'client_oid': 'bgx7-old', 'created_at': now - 500},
            {'symbol': 'LTCUSDT', 'state': 'FILLED', 'order_id': 'new',
             'client_oid': 'bgx7-new', 'created_at': now + 0.01},
        ]
        engine = SimpleNamespace(orders=SimpleNamespace(snapshot=lambda: rows))
        found = _opening_order_for_position(engine, 'LTCUSDT', now)
        self.assertEqual(found['order_id'], 'new')

    async def test_accounting_join_requires_exact_opening_order_id_and_time(self):
        open_ms = int(time.time() * 1000)
        row = {'symbol': 'LTCUSDTM', 'openTime': open_ms}
        lineage = {
            'version': 2,
            'symbol': 'LTCUSDT',
            'order_id': 'open-1',
            'client_oid': 'bgx7-open-1',
            'entry': 54.37,
            'score': 73.0,
            'nexus': 'APPROVE',
            'regime': 'RANGE',
            'entry_type': 'PULLBACK',
            'order_created_at_ms': open_ms - 500,
            'captured_at_ms': open_ms + 500,
        }
        with patch('bot.exchange_accounting_evidence.db.load_key_value',
                   new=AsyncMock(return_value=json.dumps(lineage))):
            good = await _load_lineage_for_opening_orders(['open-1'], row)
            self.assertEqual(good['nexus'], 'APPROVE')
            self.assertIsNone(await _load_lineage_for_opening_orders(['different'], row))
            self.assertIsNone(await _load_lineage_for_opening_orders(['open-1', 'open-2'], row))

    async def test_stale_timestamp_or_corrupt_lineage_is_never_attached(self):
        open_ms = int(time.time() * 1000)
        row = {'symbol': 'ETHUSDTM', 'openTime': open_ms}
        stale = {
            'version': 2, 'symbol': 'ETHUSDT', 'order_id': 'eth-open',
            'order_created_at_ms': open_ms - 3600000,
            'captured_at_ms': open_ms - 3590000,
        }
        for raw in (None, '{}', '{bad json', json.dumps(stale)):
            with self.subTest(raw=raw):
                with patch('bot.exchange_accounting_evidence.db.load_key_value',
                           new=AsyncMock(return_value=raw)):
                    self.assertIsNone(
                        await _load_lineage_for_opening_orders(['eth-open'], row)
                    )


if __name__ == '__main__':
    unittest.main()
