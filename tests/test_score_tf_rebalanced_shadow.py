import unittest

from bot import score_tf_rebalanced_shadow as shadow


class ScoreTfRebalancedShadowTests(unittest.TestCase):
    def _score(self, trend, vol, momentum, atr, struct):
        return {
            "ok": True,
            "total": trend + vol + momentum + atr + struct,
            "trend_s": trend,
            "vol_s": vol,
            "momentum_s": momentum,
            "atr_s": atr,
            "struct_s": struct,
        }

    def test_rebalanced_profile_is_normalized_to_100(self):
        perfect = self._score(30, 20, 20, 15, 15)
        self.assertEqual(shadow.rebalanced_score(perfect), 100.0)

    def test_rebalance_reduces_trend_volume_concentration(self):
        concentrated = self._score(30, 20, 0, 0, 0)
        self.assertEqual(concentrated["total"], 50)
        self.assertEqual(shadow.rebalanced_score(concentrated), 40.0)

    def test_rebalance_rewards_independent_volatility_structure_quality(self):
        diversified = self._score(15, 10, 10, 15, 15)
        self.assertEqual(diversified["total"], 65)
        self.assertGreater(shadow.rebalanced_score(diversified), 65.0)

    def test_assess_keeps_non_score_failures_authoritative(self):
        s = self._score(30, 20, 20, 15, 15)
        result = shadow.assess(
            s4h=s, s1h=s, s15=s,
            failures=["SCORE_4H", "ENTRY_TYPE"],
            thresholds={"min_4h": 60, "min_1h": 65, "min_15m": 80, "min_combined": 72},
        )
        self.assertTrue(result["score_family_cleared"])
        self.assertFalse(result["all_failures_cleared"])
        self.assertEqual(result["remaining_failures"], ["ENTRY_TYPE"])

    def test_assess_does_not_mutate_inputs(self):
        s4 = self._score(7, 2, 13, 8, 10)
        s1 = self._score(27, 4, 11, 8, 15)
        s15 = self._score(10, 4, 20, 8, 15)
        before = (dict(s4), dict(s1), dict(s15))
        shadow.assess(
            s4h=s4, s1h=s1, s15=s15,
            failures=["SCORE_4H", "SCORE_15M", "SCORE_COMBINED"],
            thresholds={"min_4h": 60, "min_1h": 65, "min_15m": 80, "min_combined": 72},
        )
        self.assertEqual((s4, s1, s15), before)


if __name__ == "__main__":
    unittest.main()
