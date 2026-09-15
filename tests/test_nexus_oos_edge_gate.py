import math
import unittest

from bot.nexus_oos_edge_gate import (
    CandidateOutcome,
    build_edge_report,
    cost_stress_report,
    edge_promotion_decision,
    evidence_status,
)


def _row(i, *, approved, known=True, r=0.0):
    return CandidateOutcome(
        timestamp=float(i),
        approved=approved,
        baseline_eligible=True,
        confidence=0.8 if approved else 0.45,
        outcome_known=known,
        r_multiple=float(r) if known else None,
    )


class NexusOOSEdgeGateTests(unittest.TestCase):
    def test_live_approved_only_evidence_cannot_prove_incremental_edge(self):
        rows = [_row(i, approved=True, known=True, r=0.6) for i in range(120)]
        rep = build_edge_report(rows, bootstrap_samples=200)
        ok, blockers = edge_promotion_decision(
            rep,
            min_baseline_samples=100,
            min_approved_samples=75,
            min_rejected_samples=25,
        )
        self.assertFalse(ok)
        self.assertIn("INSUFFICIENT_REJECTED_COUNTERFACTUAL_SAMPLE", blockers)
        self.assertEqual(
            evidence_status(
                rep,
                min_baseline_samples=100,
                min_approved_samples=75,
                min_rejected_samples=25,
            ),
            "AI_EDGE_NOT_PROVEN",
        )

    def test_missing_rejected_outcomes_blocks_even_with_many_candidates(self):
        rows = []
        for i in range(300):
            if i % 2:
                rows.append(_row(i, approved=True, known=True, r=0.5))
            else:
                rows.append(_row(i, approved=False, known=False))
        rep = build_edge_report(rows, bootstrap_samples=200)
        ok, blockers = edge_promotion_decision(
            rep,
            min_baseline_samples=100,
            min_approved_samples=75,
            min_rejected_samples=75,
        )
        self.assertFalse(ok)
        self.assertIn("INSUFFICIENT_REJECTED_COUNTERFACTUAL_SAMPLE", blockers)
        self.assertIn("COUNTERFACTUAL_COVERAGE_TOO_LOW", blockers)

    def test_positive_oos_uplift_can_pass_when_counterfactuals_are_complete(self):
        rows = []
        for i in range(240):
            approved = i % 2 == 0
            rows.append(_row(i, approved=approved, known=True, r=1.0 if approved else -1.0))
        rep = build_edge_report(rows, bootstrap_samples=1200, seed=11)
        ok, blockers = edge_promotion_decision(
            rep,
            min_baseline_samples=200,
            min_approved_samples=100,
            min_rejected_samples=100,
            min_counterfactual_coverage=0.95,
        )
        self.assertTrue(ok, blockers)
        self.assertGreater(rep.expectancy_uplift_r, 0)
        self.assertGreater(rep.bootstrap_ci_low_r, 0)
        self.assertEqual(rep.baseline_profit_factor, 1.0)
        self.assertTrue(math.isinf(rep.nexus_profit_factor))
        self.assertEqual(rep.baseline_win_rate, 0.5)
        self.assertEqual(rep.nexus_win_rate, 1.0)

    def test_paired_bootstrap_preserves_approved_subset_dependence(self):
        rows = [
            _row(i, approved=True, known=True, r=(-2.0 + (i % 9) * 0.5))
            for i in range(180)
        ]
        rep = build_edge_report(rows, bootstrap_samples=1000, seed=17)
        self.assertAlmostEqual(rep.expectancy_uplift_r, 0.0, places=12)
        self.assertAlmostEqual(rep.bootstrap_ci_low_r, 0.0, places=12)
        self.assertAlmostEqual(rep.bootstrap_ci_high_r, 0.0, places=12)

    def test_no_uplift_fails_closed(self):
        rows = [_row(i, approved=(i % 2 == 0), known=True, r=0.2) for i in range(240)]
        rep = build_edge_report(rows, bootstrap_samples=500)
        ok, blockers = edge_promotion_decision(
            rep,
            min_baseline_samples=200,
            min_approved_samples=100,
            min_rejected_samples=100,
        )
        self.assertFalse(ok)
        self.assertIn("NO_POSITIVE_EXPECTANCY_UPLIFT", blockers)
        self.assertIn("UPLIFT_NOT_STATISTICALLY_POSITIVE", blockers)

    def test_economic_metrics_capture_asymmetric_quality(self):
        rows = [
            _row(1, approved=True, r=2.0),
            _row(2, approved=True, r=-0.5),
            _row(3, approved=False, r=-1.0),
            _row(4, approved=False, r=-1.0),
        ]
        rep = build_edge_report(rows, bootstrap_samples=100, seed=3)
        self.assertAlmostEqual(rep.baseline_profit_factor, 0.8)
        self.assertAlmostEqual(rep.nexus_profit_factor, 4.0)
        self.assertAlmostEqual(rep.baseline_win_rate, 0.25)
        self.assertAlmostEqual(rep.nexus_win_rate, 0.5)

    def test_cost_stress_is_monotonic_and_research_only(self):
        rows = [
            _row(1, approved=True, r=0.6),
            _row(2, approved=True, r=0.2),
            _row(3, approved=False, r=-0.4),
            _row(4, approved=False, r=-0.2),
        ]
        stress = cost_stress_report(rows, extra_cost_r=(0.0, 0.10, 0.25))
        self.assertEqual(len(stress), 3)
        self.assertGreater(stress[0]["nexus_expectancy_r"], stress[1]["nexus_expectancy_r"])
        self.assertGreater(stress[1]["nexus_expectancy_r"], stress[2]["nexus_expectancy_r"])
        self.assertEqual({row["execution_effect"] for row in stress}, {"NONE"})

    def test_invalid_cost_stress_fails_closed(self):
        rows = [_row(1, approved=True, r=1.0)]
        with self.assertRaises(ValueError):
            cost_stress_report(rows, extra_cost_r=(-0.1,))

    def test_unknown_outcome_cannot_carry_r_multiple(self):
        with self.assertRaises(ValueError):
            CandidateOutcome(1.0, False, True, 0.5, False, 1.0).validate()


if __name__ == "__main__":
    unittest.main()
