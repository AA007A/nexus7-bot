import unittest

from bot.nexus_oos_edge_gate import CandidateOutcome
from bot.nexus_oos_temporal_block_bootstrap import temporal_block_bootstrap


DAY_MS = 86_400_000


def row(day, idx, *, approved, r):
    return CandidateOutcome(
        timestamp=float(day * DAY_MS + idx * 60_000),
        approved=approved,
        baseline_eligible=True,
        confidence=0.8 if approved else 0.4,
        outcome_known=True,
        r_multiple=float(r),
    )


class TemporalBlockBootstrapTests(unittest.TestCase):
    def test_all_approved_has_exact_zero_uplift_and_ci(self):
        rows = []
        for day in range(12):
            for idx in range(4):
                rows.append(row(day, idx, approved=True, r=(-1.5 + 0.5 * idx)))
        rep = temporal_block_bootstrap(rows, bootstrap_samples=600, seed=1)
        self.assertTrue(rep.available)
        self.assertAlmostEqual(rep.expectancy_uplift_r, 0.0, places=12)
        self.assertAlmostEqual(rep.ci_low_r, 0.0, places=12)
        self.assertAlmostEqual(rep.ci_high_r, 0.0, places=12)
        self.assertFalse(rep.ci_strictly_positive)

    def test_strong_repeated_daily_selection_edge_survives_blocks(self):
        rows = []
        for day in range(24):
            rows.append(row(day, 0, approved=True, r=1.0))
            rows.append(row(day, 1, approved=True, r=0.8))
            rows.append(row(day, 2, approved=False, r=-0.8))
            rows.append(row(day, 3, approved=False, r=-1.0))
        rep = temporal_block_bootstrap(rows, bootstrap_samples=1000, seed=3)
        self.assertTrue(rep.available)
        self.assertGreater(rep.expectancy_uplift_r, 0.0)
        self.assertGreater(rep.ci_low_r, 0.0)
        self.assertTrue(rep.ci_strictly_positive)
        self.assertEqual(rep.unique_buckets, 24)

    def test_insufficient_time_buckets_fails_closed(self):
        rows = []
        for day in range(3):
            rows.append(row(day, 0, approved=True, r=1.0))
            rows.append(row(day, 1, approved=False, r=-1.0))
        rep = temporal_block_bootstrap(rows, bootstrap_samples=200)
        self.assertFalse(rep.available)
        self.assertIsNone(rep.ci_low_r)
        self.assertIsNone(rep.ci_high_r)
        self.assertFalse(rep.ci_strictly_positive)

    def test_epoch_seconds_are_supported(self):
        base = 1_780_000_000.0
        rows = []
        for day in range(10):
            ts = base + day * 86_400
            rows.append(CandidateOutcome(ts, True, True, 0.8, True, 1.0))
            rows.append(CandidateOutcome(ts + 60, False, True, 0.4, True, -1.0))
        rep = temporal_block_bootstrap(rows, bootstrap_samples=400, seed=8)
        self.assertTrue(rep.available)
        self.assertGreaterEqual(rep.unique_buckets, 9)


if __name__ == "__main__":
    unittest.main()
