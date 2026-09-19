import unittest

from bot.nexus_oos_edge_gate import CandidateOutcome
from bot.nexus_oos_robustness import analyze_robustness, robustness_promotion_blockers


def row(ts, approved, r):
    return CandidateOutcome(
        timestamp=float(ts),
        approved=bool(approved),
        baseline_eligible=True,
        confidence=0.8 if approved else 0.4,
        outcome_known=True,
        r_multiple=float(r),
    )


class NexusOOSRobustnessTests(unittest.TestCase):
    def test_detects_stable_positive_uplift_across_symbols_and_time(self):
        reports = []
        ts = 0
        for symbol in ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"):
            rows = []
            for i in range(160):
                approved = (i % 2 == 0)
                rows.append(row(ts, approved, 1.0 if approved else -0.8))
                ts += 1
            reports.append({"symbol": symbol, "candidates": rows})

        rep = analyze_robustness(reports, temporal_folds=4)
        summary = rep["summary"]
        self.assertTrue(summary["stable_positive_point_estimate"])
        self.assertEqual(summary["leave_one_symbol_out_positive_uplift"], 4)
        self.assertGreaterEqual(summary["temporal_folds_positive_uplift"], 3)
        self.assertFalse(summary["promotion_authority"])
        self.assertEqual(summary["promotion_role"], "BLOCK_ONLY")
        self.assertEqual(summary["execution_effect"], "NONE")
        self.assertEqual(robustness_promotion_blockers(rep), ())

    def test_exposes_single_symbol_concentration_as_promotion_blocker(self):
        reports = []
        for s_idx, symbol in enumerate(("BTCUSDT", "ETHUSDT", "SOLUSDT")):
            rows = []
            for i in range(180):
                approved = (i % 2 == 0)
                if s_idx == 0:
                    r = 2.0 if approved else -2.0
                else:
                    r = -0.3 if approved else 0.3
                rows.append(row(s_idx * 10000 + i, approved, r))
            reports.append({"symbol": symbol, "candidates": rows})

        rep = analyze_robustness(reports, temporal_folds=3)
        summary = rep["summary"]
        self.assertFalse(summary["stable_positive_point_estimate"])
        self.assertLess(summary["leave_one_symbol_out_positive_uplift"], 3)
        self.assertIn("BTCUSDT", rep["leave_one_symbol_out"])
        blockers = robustness_promotion_blockers(rep, min_temporal_folds=3)
        self.assertIn("SYMBOL_CONCENTRATION_RISK", blockers)

    def test_temporal_instability_blocks_promotion(self):
        reports = []
        ts = 0
        for symbol in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
            rows = []
            for i in range(240):
                approved = (i % 2 == 0)
                # First half: NEXUS selection helps. Second half: selection hurts.
                positive_regime = i < 120
                if positive_regime:
                    r = 1.0 if approved else -0.8
                else:
                    r = -0.8 if approved else 1.0
                rows.append(row(ts, approved, r))
                ts += 1
            reports.append({"symbol": symbol, "candidates": rows})

        rep = analyze_robustness(reports, temporal_folds=4)
        blockers = robustness_promotion_blockers(rep)
        self.assertIn("TEMPORAL_EDGE_UNSTABLE", blockers)

    def test_empty_input_is_non_authoritative_and_incomplete(self):
        rep = analyze_robustness([], temporal_folds=4)
        self.assertIsNone(rep["pooled"])
        self.assertFalse(rep["summary"]["stable_positive_point_estimate"])
        self.assertFalse(rep["summary"]["promotion_authority"])
        self.assertEqual(rep["summary"]["promotion_role"], "BLOCK_ONLY")
        self.assertIn("ROBUSTNESS_EVIDENCE_INCOMPLETE", robustness_promotion_blockers(rep))

    def test_invalid_robustness_thresholds_fail_closed(self):
        rep = analyze_robustness([], temporal_folds=4)
        with self.assertRaises(ValueError):
            robustness_promotion_blockers(rep, min_temporal_positive_fraction=0.0)
        with self.assertRaises(ValueError):
            robustness_promotion_blockers(rep, min_temporal_folds=1)


if __name__ == "__main__":
    unittest.main()
