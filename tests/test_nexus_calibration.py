import unittest

from bot.nexus_calibration import (
    brier_score,
    calibration_report,
    expected_calibration_error,
    reliability_bins,
)


class NexusCalibrationTests(unittest.TestCase):
    def test_perfect_predictions_have_zero_brier_and_ece(self):
        rows = [(0.0, 0), (1.0, 1)]
        self.assertEqual(brier_score(rows), 0.0)
        self.assertEqual(expected_calibration_error(rows), 0.0)

    def test_known_brier_score(self):
        rows = [(0.75, 1), (0.25, 0)]
        self.assertAlmostEqual(brier_score(rows), 0.0625)

    def test_reliability_bins_report_observed_rate(self):
        rows = [(0.61, 1), (0.64, 0), (0.68, 1)]
        bins = reliability_bins(rows, bins=10)
        self.assertEqual(len(bins), 1)
        self.assertEqual(bins[0]["count"], 3)
        self.assertAlmostEqual(bins[0]["observed_win_rate"], 2 / 3)

    def test_empty_report_does_not_invent_calibration(self):
        report = calibration_report([])
        self.assertEqual(report["sample_size"], 0)
        self.assertIsNone(report["brier_score"])
        self.assertIsNone(report["ece"])
        self.assertEqual(report["execution_effect"], "NONE")

    def test_invalid_probability_fails_closed(self):
        with self.assertRaises(ValueError):
            calibration_report([(1.2, 1)])


if __name__ == "__main__":
    unittest.main()
