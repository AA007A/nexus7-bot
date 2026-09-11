import unittest

from bot.oos_calibration_readiness import (
    build_readiness,
    compact_readiness_log,
    normalize_completed_rows,
)
from bot.oos_model_validation import ValidationRow


class OOSCalibrationReadinessTests(unittest.TestCase):
    def test_reconstructs_ev_probability_and_counts_invalid(self):
        rows, invalid = normalize_completed_rows([
            ("100", 80.0, 1, 1.2),
            ("bad", 50.0, 0, -0.5),
        ])
        self.assertEqual(len(rows), 1)
        self.assertEqual(invalid, 1)
        self.assertAlmostEqual(rows[0].confidence, 0.66)

    def test_small_sample_is_fail_closed(self):
        rows = [ValidationRow(float(i), 0.8, 1, 1.0) for i in range(20)]
        snap = build_readiness(rows)
        self.assertFalse(snap["ready"])
        self.assertIn("INSUFFICIENT_FOR_WALK_FORWARD", snap["blockers"])
        self.assertEqual(snap["decision_effect"], "NONE")
        self.assertEqual(snap["execution_effect"], "NONE")

    def test_good_chronological_oos_sample_can_pass_analytical_gate(self):
        rows = []
        for i in range(165):
            win = 1 if i % 2 == 0 else 0
            rows.append(ValidationRow(
                timestamp=float(i),
                confidence=0.95 if win else 0.05,
                outcome=win,
                r_multiple=1.0 if win else -0.5,
            ))
        snap = build_readiness(rows)
        self.assertEqual(snap["folds"], 5)
        self.assertEqual(snap["oos_n"], 100)
        self.assertTrue(snap["ready"])
        self.assertEqual(snap["blockers"], ())
        self.assertGreater(snap["report"]["expectancy_r"], 0.0)

    def test_log_is_explicitly_non_decisional(self):
        text = compact_readiness_log(build_readiness([]))
        self.assertIn("ready=False", text)
        self.assertIn("decision_effect=NONE", text)
        self.assertIn("execution_effect=NONE", text)


if __name__ == "__main__":
    unittest.main()
