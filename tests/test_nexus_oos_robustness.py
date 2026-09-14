import unittest

from bot.nexus_oos_edge_gate import CandidateOutcome
from bot.nexus_oos_robustness import analyze_robustness


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
        self.assertEqual(summary["execution_effect"], "NONE")

    def test_exposes_single_symbol_concentration(self):
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

    def test_empty_input_is_non_authoritative(self):
        rep = analyze_robustness([], temporal_folds=4)
        self.assertIsNone(rep["pooled"])
        self.assertFalse(rep["summary"]["stable_positive_point_estimate"])
        self.assertFalse(rep["summary"]["promotion_authority"])


if __name__ == "__main__":
    unittest.main()
