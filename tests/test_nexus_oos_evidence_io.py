import tempfile
import unittest
from pathlib import Path

from bot.nexus_oos_edge_gate import CandidateOutcome
from bot.nexus_oos_evidence_io import dataset_csv, dataset_sha256, write_evidence_bundle


class NexusOOSEvidenceIOTests(unittest.TestCase):
    def _rows(self):
        return [
            CandidateOutcome(2.0, False, True, 0.4, True, -1.0),
            CandidateOutcome(1.0, True, True, 0.8, True, 1.0),
        ]

    def test_canonical_csv_is_chronological_and_hash_stable(self):
        rows = self._rows()
        text_a = dataset_csv(rows)
        text_b = dataset_csv(reversed(rows))
        self.assertEqual(text_a, text_b)
        self.assertEqual(dataset_sha256(rows), dataset_sha256(reversed(rows)))
        self.assertLess(text_a.index("1,1,1"), text_a.index("2,0,1"))

    def test_duplicate_candidate_timestamp_fails_closed(self):
        rows = self._rows() + [CandidateOutcome(1.0, False, True, 0.2, True, -0.5)]
        with self.assertRaises(ValueError):
            dataset_csv(rows)

    def test_bundle_keeps_not_proven_status_when_sample_is_insufficient(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = write_evidence_bundle(
                self._rows(),
                tmp,
                bootstrap_samples=100,
                gate_kwargs={
                    "min_baseline_samples": 200,
                    "min_approved_samples": 75,
                    "min_rejected_samples": 75,
                },
            )
            self.assertEqual(payload["status"], "AI_EDGE_NOT_PROVEN")
            self.assertFalse(payload["proven"])
            self.assertTrue(payload["blockers"])
            self.assertEqual(len(payload["dataset_sha256"]), 64)
            self.assertTrue((Path(tmp) / "nexus_oos_candidates.csv").is_file())
            self.assertTrue((Path(tmp) / "nexus_oos_edge_report.json").is_file())

    def test_bundle_can_prove_only_complete_positive_counterfactual_evidence(self):
        rows = []
        for i in range(240):
            approved = i % 2 == 0
            rows.append(
                CandidateOutcome(
                    float(i), approved, True, 0.8 if approved else 0.4,
                    True, 1.0 if approved else -1.0,
                )
            )
        with tempfile.TemporaryDirectory() as tmp:
            payload = write_evidence_bundle(
                rows,
                tmp,
                bootstrap_samples=1200,
                seed=11,
                gate_kwargs={
                    "min_baseline_samples": 200,
                    "min_approved_samples": 100,
                    "min_rejected_samples": 100,
                    "min_counterfactual_coverage": 0.95,
                },
            )
            self.assertEqual(payload["status"], "AI_EDGE_PROVEN")
            self.assertTrue(payload["proven"])
            self.assertEqual(payload["blockers"], [])
            self.assertGreater(payload["report"]["bootstrap_ci_low_r"], 0.0)


if __name__ == "__main__":
    unittest.main()
