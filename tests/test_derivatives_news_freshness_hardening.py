import time
import unittest

from bot import derivatives_news_freshness_hardening as hardening


class _ScoringStub:
    _MACRO_KEYWORDS = [
        "fomc", "cpi", "nfp", "interest rate", "fed ", "federal reserve",
        "inflation", "jobs report", "gdp",
    ]
    _BULLISH_KW = [
        "bull", "surge", "rally", "breakout", "ath", "adoption",
        "etf", "approval", "buy", "long", "support",
    ]
    _BEARISH_KW = [
        "bear", "crash", "dump", "ban", "hack", "sell", "short",
        "regulation", "sec", "lawsuit", "collapse",
    ]


class NewsNegationSemanticsTests(unittest.TestCase):
    def test_etf_not_approved_is_bearish(self):
        classification, confidence, is_macro = hardening.classify_headline(
            "SEC says spot Bitcoin ETF not approved after review",
            _ScoringStub,
        )
        self.assertEqual(classification, "BEARISH")
        self.assertGreaterEqual(confidence, 0.6)
        self.assertFalse(is_macro)

    def test_etf_approved_is_bullish(self):
        classification, confidence, _ = hardening.classify_headline(
            "SEC approves spot Bitcoin ETF for trading",
            _ScoringStub,
        )
        self.assertEqual(classification, "BULLISH")
        self.assertGreaterEqual(confidence, 0.6)

    def test_negated_hack_is_not_bearish(self):
        classification, confidence, _ = hardening.classify_headline(
            "Exchange says wallets were not hacked and funds are safe",
            _ScoringStub,
        )
        self.assertNotEqual(classification, "BEARISH")
        self.assertLessEqual(confidence, 0.5)

    def test_rejected_etf_approval_is_bearish_even_with_sec_keyword(self):
        classification, confidence, _ = hardening.classify_headline(
            "Bitcoin ETF approval rejected by SEC",
            _ScoringStub,
        )
        self.assertEqual(classification, "BEARISH")
        self.assertGreaterEqual(confidence, 0.6)

    def test_macro_detection_is_preserved(self):
        classification, _, is_macro = hardening.classify_headline(
            "Fed CPI report shows inflation cooling as Bitcoin rallies",
            _ScoringStub,
        )
        self.assertEqual(classification, "BULLISH")
        self.assertTrue(is_macro)


class DerivativesFreshnessTests(unittest.TestCase):
    def test_stale_field_is_excluded_from_score(self):
        now = time.time()
        cache = {
            "ls_ratio": 1.60,
            "taker_buy_ratio": 1.40,
            "_field_fetched_at": {
                "ls_ratio": now - hardening.DERIVATIVES_TTL_S - 1,
                "taker_buy_ratio": now - 30,
            },
        }
        parts = hardening._derivative_components(cache, now)
        self.assertEqual(parts["all_score"], 18)
        self.assertEqual(parts["fresh_score"], 8)
        self.assertEqual(parts["fresh_fields"], ["taker_buy_ratio"])
        self.assertEqual(parts["stale_fields"], ["ls_ratio"])

    def test_all_fresh_derivatives_keep_full_component(self):
        now = time.time()
        cache = {
            "ls_ratio": 0.65,
            "taker_buy_ratio": 0.75,
            "_field_fetched_at": {
                "ls_ratio": now - 10,
                "taker_buy_ratio": now - 20,
            },
        }
        parts = hardening._derivative_components(cache, now)
        self.assertEqual(parts["all_score"], -18)
        self.assertEqual(parts["fresh_score"], -18)
        self.assertFalse(parts["stale_fields"])

    def test_missing_timestamp_never_counts_as_fresh(self):
        cache = {"ls_ratio": 1.7, "taker_buy_ratio": 1.5}
        parts = hardening._derivative_components(cache, time.time())
        self.assertEqual(parts["all_score"], 18)
        self.assertEqual(parts["fresh_score"], 0)
        self.assertCountEqual(
            parts["stale_fields"], ["ls_ratio", "taker_buy_ratio"]
        )

    def test_future_timestamp_fails_freshness(self):
        now = time.time()
        cache = {
            "ls_ratio": 1.7,
            "_field_fetched_at": {
                "ls_ratio": now + hardening._MAX_FUTURE_SKEW_S + 1,
            },
        }
        self.assertFalse(hardening._field_is_fresh(cache, "ls_ratio", now))


if __name__ == "__main__":
    unittest.main()
