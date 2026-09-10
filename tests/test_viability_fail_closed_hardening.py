import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from bot import viability_fail_closed_hardening as hardening


class _Engine:
    def __init__(self):
        self.instruments = {"BTCUSDT": {"minQty": 1, "multiplier": 0.001}}
        self.viable_symbols = []
        self.risk = SimpleNamespace(balance=100.0)
        self.client = SimpleNamespace(
            get_all_tickers=AsyncMock(return_value=[{"symbol": "BTCUSDT", "lastPrice": "10000"}]),
            get_cached_ticker=Mock(return_value={}),
        )


class ViabilityFailClosedTests(unittest.IsolatedAsyncioTestCase):
    async def test_valid_symbol_can_pass(self):
        engine = _Engine()
        with patch.object(hardening, "__name__", hardening.__name__):
            ok = await hardening._filter_viable_symbols_fail_closed(engine)
        self.assertTrue(ok)
        self.assertIn("BTCUSDT", engine.viable_symbols)

    async def test_ticker_exception_blocks_everything(self):
        engine = _Engine()
        engine.client.get_all_tickers.side_effect = RuntimeError("network")
        ok = await hardening._filter_viable_symbols_fail_closed(engine)
        self.assertFalse(ok)
        self.assertEqual(engine.viable_symbols, [])

    async def test_missing_instrument_metadata_blocks(self):
        engine = _Engine()
        engine.instruments = {}
        ok = await hardening._filter_viable_symbols_fail_closed(engine)
        self.assertFalse(ok)
        self.assertEqual(engine.viable_symbols, [])

    async def test_unknown_price_blocks_symbol(self):
        engine = _Engine()
        engine.client.get_all_tickers.return_value = []
        engine.client.get_cached_ticker.return_value = {}
        ok = await hardening._filter_viable_symbols_fail_closed(engine)
        self.assertFalse(ok)
        self.assertEqual(engine.viable_symbols, [])


if __name__ == "__main__":
    unittest.main()
