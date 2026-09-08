import unittest

from bot.nexus_calibration import (
    CalibrationRow,
    calibration_report,
    out_of_sample_expectancy,
    purged_walk_forward_splits,
)


class NexusCalibrationTests(unittest.TestCase):
    def test_brier_and_buckets(self):
        rows = [
            CalibrationRow(60, True, 2.0),
            CalibrationRow(65, False, -1.0),
            CalibrationRow(80, True, 1.5),
            CalibrationRow(85, True, 1.0),
        ]
        rep = calibration_report(rows, bucket_width=10)
        self.assertEqual(rep.n, 4)
        self.assertGreater(rep.brier_score, 0)
        self.assertEqual(len(rep.buckets), 2)
        self.assertAlmostEqual(rep.buckets[0].expectancy_r, 0.5)

    def test_walk_forward_is_chronological_and_purged(self):
        splits = purged_walk_forward_splits(
            30, train_size=10, test_size=5, purge_size=2, step=5
        )
        self.assertTrue(splits)
        for train, test in splits:
            self.assertLess(max(train), min(test))
            self.assertEqual(min(test) - max(train) - 1, 2)

    def test_oos_expectancy_uses_only_requested_test_indices(self):
        rows = [
            CalibrationRow(50, True, 10),
            CalibrationRow(50, False, -10),
            CalibrationRow(50, True, 1),
            CalibrationRow(50, False, -1),
        ]
        self.assertEqual(out_of_sample_expectancy(rows, [2, 3]), 0.0)

    def test_invalid_confidence_rejected(self):
        with self.assertRaises(ValueError):
            calibration_report([CalibrationRow(101, True, 1.0)])


if __name__ == "__main__":
    unittest.main()
