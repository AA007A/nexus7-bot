import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.post_trade_forensics import _lineage
from bot.exchange_accounting_evidence import _load_lineage


class PostTradeLineageTests(unittest.IsolatedAsyncioTestCase):
    def test_lineage_captures_entry_decision_context(self):
        sig = SimpleNamespace(symbol='ETHUSDTM', direction='LONG', entry=2500.0,
                              score=81, regime='TRENDING', entry_type='PULLBACK')
        result = _lineage(sig, {'decision': 'APPROVE'})
        self.assertEqual(result['symbol'], 'ETHUSDTM')
        self.assertEqual(result['nexus'], 'APPROVE')
        self.assertEqual(result['regime'], 'TRENDING')
        self.assertEqual(result['entry_type'], 'PULLBACK')
        self.assertEqual(result['score'], 81.0)

    async def test_exchange_lineage_requires_matching_symbol_and_entry(self):
        row = {'symbol': 'ETHUSDTM', 'openPrice': '2500'}
        good = dict(version=1, symbol='ETHUSDTM', entry=2500.0, nexus='APPROVE')
        with patch('bot.exchange_accounting_evidence.db.load_key_value',
                   new=AsyncMock(return_value=json.dumps(good))):
            self.assertEqual((await _load_lineage(row))['nexus'], 'APPROVE')

        stale = dict(good, entry=2400.0)
        with patch('bot.exchange_accounting_evidence.db.load_key_value',
                   new=AsyncMock(return_value=json.dumps(stale))):
            self.assertIsNone(await _load_lineage(row))

    async def test_missing_or_invalid_lineage_never_fabricates_context(self):
        row = {'symbol': 'ETHUSDTM', 'openPrice': '2500'}
        for raw in (None, '{}', '{bad json'):
            with self.subTest(raw=raw):
                with patch('bot.exchange_accounting_evidence.db.load_key_value',
                           new=AsyncMock(return_value=raw)):
                    if raw == '{bad json':
                        with self.assertRaises(json.JSONDecodeError):
                            await _load_lineage(row)
                    else:
                        self.assertIsNone(await _load_lineage(row))
