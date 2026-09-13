import unittest

from bot.nexus_oos_evidence_dataset import ReplayObservation, audit_edge_dataset, build_candidate_dataset


class TestNexusOOSEvidenceDataset(unittest.TestCase):
    def test_rejected_unknown_outcome_is_not_imputed(self):
        rows = [
            ReplayObservation(1, True, True, 0.8, 100, 99, 102, 0.1),
            ReplayObservation(2, False, True, 0.2, 100, 99, None, 0.1),
        ]
        data = build_candidate_dataset(rows)
        self.assertTrue(data[0].outcome_known)
        self.assertFalse(data[1].outcome_known)
        self.assertIsNone(data[1].r_multiple)
        rep, ok, blockers = audit_edge_dataset(
            rows,
            min_baseline_samples=1,
            min_approved_samples=1,
            min_rejected_samples=1,
            min_counterfactual_coverage=1.0,
        )
        self.assertFalse(ok)
        self.assertLess(rep.counterfactual_coverage, 1.0)
        self.assertIn("INSUFFICIENT_REJECTED_COUNTERFACTUAL_SAMPLE", blockers)
        self.assertIn("COUNTERFACTUAL_COVERAGE_TOO_LOW", blockers)

    def test_costs_are_subtracted_in_r_space(self):
        row = ReplayObservation(1, True, True, 0.8, 100, 99, 102, 0.25)
        candidate = row.to_candidate()
        self.assertAlmostEqual(candidate.r_multiple, 1.75)

    def test_short_direction_is_normalized(self):
        row = ReplayObservation(1, True, True, 0.8, 100, 101, 98, 0.10)
        self.assertAlmostEqual(row.to_candidate().r_multiple, 1.90)

    def test_duplicate_timestamp_fails_closed(self):
        rows = [
            ReplayObservation(1, True, True, 0.8, 100, 99, 101),
            ReplayObservation(1, False, True, 0.2, 100, 99, 98),
        ]
        with self.assertRaises(ValueError):
            build_candidate_dataset(rows)


if __name__ == "__main__":
    unittest.main()
