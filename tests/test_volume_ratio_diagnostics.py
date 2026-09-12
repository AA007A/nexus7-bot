import unittest
from bot import market_data_integrity as integrity
from bot.volume_ratio_diagnostics import _closed_ratio


class VolumeRatioDiagnosticsTests(unittest.TestCase):
    def _candle(self, ts_ms, v):
        return {"ts": ts_ms, "o": "1", "h": "2", "l": ".5", "c": "1.5", "v": str(v)}

    def test_ratio_uses_latest_confirmed_and_prior_20(self):
        base = 1_800_000_000_000
        width = 15 * 60 * 1000
        candles = [self._candle(base + i * width, 100.0) for i in range(20)]
        candles.append(self._candle(base + 20 * width, 2.0))
        result = _closed_ratio(candles, integrity)
        self.assertIsNotNone(result)
        current, avg20, ratio, ts = result
        self.assertEqual(current, 2.0)
        self.assertEqual(avg20, 100.0)
        self.assertAlmostEqual(ratio, 0.02)
        self.assertEqual(ts, base + 20 * width)

    def test_invalid_or_short_history_is_diagnostic_none(self):
        base = 1_800_000_000_000
        width = 15 * 60 * 1000
        short = [self._candle(base + i * width, 10.0) for i in range(20)]
        self.assertIsNone(_closed_ratio(short, integrity))
        bad = [self._candle(base + i * width, 10.0) for i in range(21)]
        bad[-1]["v"] = "nan"
        self.assertIsNone(_closed_ratio(bad, integrity))
