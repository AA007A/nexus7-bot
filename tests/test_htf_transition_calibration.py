import unittest

from bot import htf_transition_calibration as calibration


class HTFTransitionCalibrationTests(unittest.TestCase):
    def test_small_sample_never_review_ready(self):
        metrics = calibration.compute_readiness([0.8] * 20)
        self.assertFalse(metrics["review_ready"])
        self.assertEqual(metrics["resolved_non_ambiguous"], 20)

    def test_positive_but_uncertain_sample_not_ready(self):
        values = [2.0, -1.9] * 20
        metrics = calibration.compute_readiness(values)
        self.assertGreater(metrics["mean_net_pct"], 0)
        self.assertFalse(metrics["review_ready"])
        self.assertLessEqual(metrics["lower95_net_pct"], 0)

    def test_strong_evidence_becomes_review_ready_only(self):
        values = [1.0] * 34 + [-0.35] * 6
        metrics = calibration.compute_readiness(values)
        self.assertTrue(metrics["review_ready"])
        self.assertGreater(metrics["lower95_net_pct"], 0)
        self.assertGreaterEqual(metrics["profit_factor"], 1.20)
        self.assertEqual(metrics["execution_effect"], "NONE")

    def test_ambiguity_rate_blocks_readiness(self):
        values = [1.0] * 40
        metrics = calibration.compute_readiness(
            values, ambiguous=10, resolved_total=50
        )
        self.assertGreater(metrics["lower95_net_pct"], 0)
        self.assertFalse(metrics["review_ready"])
        self.assertGreater(metrics["ambiguous_rate"], 0.10)


if __name__ == "__main__":
    unittest.main()
