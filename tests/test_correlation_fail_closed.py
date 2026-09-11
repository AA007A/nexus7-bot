import unittest

from bot import correlation


class CorrelationFailClosedTests(unittest.TestCase):
    def setUp(self):
        correlation._closes_cache.clear()

    def test_no_open_positions_does_not_require_correlation(self):
        result = correlation.check_correlation("SOLUSDT", {})
        self.assertTrue(result["ok"])
        self.assertEqual(result["data_quality"], "NOT_REQUIRED")

    def test_missing_new_symbol_history_blocks_when_exposed(self):
        correlation.seed_closes("BTCUSDT", list(range(100, 130)))
        result = correlation.check_correlation("SOLUSDT", {"BTCUSDT": object()})
        self.assertFalse(result["ok"])
        self.assertEqual(result["data_quality"], "INSUFFICIENT")
        self.assertEqual(result["correlated_with"], "SOLUSDT")

    def test_missing_existing_position_history_blocks(self):
        correlation.seed_closes("SOLUSDT", list(range(50, 80)))
        result = correlation.check_correlation("SOLUSDT", {"BTCUSDT": object()})
        self.assertFalse(result["ok"])
        self.assertEqual(result["data_quality"], "INSUFFICIENT")
        self.assertEqual(result["correlated_with"], "BTCUSDT")

    def test_high_positive_correlation_blocks(self):
        base = [100 + i * 0.5 for i in range(30)]
        same = [200 + i * 1.0 for i in range(30)]
        correlation.seed_closes("SOLUSDT", base)
        correlation.seed_closes("BTCUSDT", same)
        result = correlation.check_correlation("SOLUSDT", {"BTCUSDT": object()})
        self.assertFalse(result["ok"])
        self.assertEqual(result["data_quality"], "OK")
        self.assertGreaterEqual(result["max_corr"], 0.70)

    def test_sufficient_low_correlation_can_pass(self):
        new = [100, 101, 100.5, 101.7, 101.1, 102.4, 101.9, 102.8, 102.1, 103.0,
               102.4, 103.3, 102.9, 103.8, 103.2, 104.0, 103.5, 104.2, 103.7, 104.5,
               104.0, 104.8, 104.2, 105.0, 104.6, 105.2, 104.8, 105.5, 105.0, 105.7]
        other = [200, 199.5, 200.4, 199.7, 200.6, 199.8, 200.9, 200.1, 201.0, 200.3,
                 201.2, 200.5, 201.3, 200.7, 201.5, 200.9, 201.7, 201.0, 201.9, 201.2,
                 202.1, 201.4, 202.2, 201.6, 202.4, 201.8, 202.5, 202.0, 202.7, 202.2]
        correlation.seed_closes("SOLUSDT", new)
        correlation.seed_closes("BTCUSDT", other)
        result = correlation.check_correlation("SOLUSDT", {"BTCUSDT": object()})
        self.assertEqual(result["data_quality"], "OK")
        self.assertIsInstance(result["ok"], bool)


if __name__ == "__main__":
    unittest.main()
