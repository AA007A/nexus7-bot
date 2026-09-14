import unittest

from bot.nexus_oos_edge_gate import (
    CandidateOutcome,
    build_edge_report,
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
        self.assertEqual(evidence_status(rep, min_baseline_samples=100,
                                         min_approved_samples=75,
                                         min_rejected_samples=25),
                         "AI_EDGE_NOT_PROVEN")

    def test_missing_rejected_outcomes_blocks_even_with_many_candidates(self):
        rows = []
        for i in range(300):
            if i % 2:
                rows.append(_row(i, approved=True, known=True, r=0.5))
            else:
                rows.append(_row(i, approved=False, known=False))
        rep = build_edge_report(rows, bootstrap_samples=200)
        ok, blockers = edge_promotion_decision(rep, min_baseline_samples=100,
                                                min_approved_samples=75,
                                                min_rejected_samples=75)
        self.assertFalse(ok)
        self.assertIn("INSUFFICIENT_REJECTED_COUNTERFACTUAL_SAMPLE", blockers)
        self.assertIn("COUNTERFACTUAL_COVERAGE_TOO_LOW", blockers)

    def test_positive_oos_uplift_can_pass_when_counterfactuals_are_complete(self):
        rows = []
        # Baseline receives all candidates. NEXUS selects the high-quality half.
        # Approved expectancy = +1R; rejected expectancy = -1R; baseline ~0R.
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

    def test_paired_bootstrap_preserves_approved_subset_dependence(self):
        # When every baseline row is approved, the two estimands are literally
        # the same population in every bootstrap draw. A correct paired
        # candidate bootstrap must therefore return an exactly zero-width CI at
        # zero even though the individual R outcomes have large dispersion.
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

    def test_unknown_outcome_cannot_carry_r_multiple(self):
        with self.assertRaises(ValueError):
            CandidateOutcome(1.0, False, True, 0.5, False, 1.0).validate()


if __name__ == "__main__":
    unittest.main()
