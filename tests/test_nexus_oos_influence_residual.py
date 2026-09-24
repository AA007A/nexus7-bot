"""Residual-dependence authority uses ESTIMATOR-ALIGNED block influence scores.

The clustered bootstrap estimates mu_A = sum(S_A,t) / sum(N_A,t) (and
mu_A - mu_B for uplift), i.e. a COUNT-weighted statistic over every calendar
block of the research timeline. Its residual-dependence check therefore uses

    psi(t) = (S_A,t - mu_A N_A,t)/mean_count_A  [- (S_B,t - mu_B N_B,t)/mean_count_B]

computed from compact aggregates (never equal-weight block means).

1. Mean influence aggregates reproduce the global approved expectancy.
2. Difference influence aggregates reproduce the global uplift.
3. Variable block counts do not turn the authority into an equal-weight block-mean statistic.
4. A nested A-subset-of-B counterexample: the equal-weight paired block delta looks clean,
   the influence score is significant, and authority fails.
5. Zero-A blocks remain in the uplift influence timeline.
6. Calendar gaps still break adjacency.
7. Tampering with sum/count aggregates is detected (point-estimate or ACF mismatch).
8. A constant influence series is NOT_ESTIMABLE.
9. A clean estimator-aligned influence series keeps authority.
"""
import copy
import math
import random
import unittest

from bot import nexus_oos_inference as inf
from bot import nexus_oos_promotion_gate as gate
from tests.test_nexus_oos_promotion_gate import (DIFF_TARGET, MEAN_TARGET, _passing_artifact,
                                                  clean_series, paired_residual, residual)

DAY = inf.DAY_MS
HOUR = 3_600_000
D0 = 1_735_689_600_000
APPROVED = lambda r: r["approved"]  # noqa: E731
BASELINE = lambda r: True  # noqa: E731    production uplift: B = all executable candidates


def _row(ts, approved, r):
    return {"ts": ts, "outcome_end_ts": ts + 3 * HOUR, "approved": approved, "r": r,
            "outcome_status": "RESOLVED"}


def _noise_rows(days=120, seed=12):
    rng = random.Random(seed)
    rows = []
    for d in range(days):
        for k in range(rng.randint(1, 4)):                    # variable A counts
            rows.append(_row(D0 + d * DAY + k * HOUR, True, rng.gauss(0.2, 0.5)))
        for k in range(6):
            rows.append(_row(D0 + d * DAY + (k + 8) * HOUR, False, rng.gauss(0.0, 0.5)))
    return rows


def _counterexample_rows(seed=9, days=45, amplitude=20.0, sd=0.1):
    """A subset of B. A's block contribution follows a slow regime s(t); A counts
    are 1 or 100 per block, so an equal-weight block mean dilutes the regime by
    1/N while the count-weighted estimator (and its influence score) keeps it."""
    rng = random.Random(seed)
    rows = []
    for t in range(days):
        n = 1 if rng.random() < 0.2 else 100
        s = math.sin(2 * math.pi * t / 20)
        for i in range(n):
            rows.append(_row(D0 + t * DAY + i * 300_000, True, amplitude * s / n + rng.gauss(0, sd)))
        for i in range(50):
            rows.append(_row(D0 + t * DAY + (i + 200) * 300_000, False, rng.gauss(0, sd)))
    return rows


def _section(name, report):
    a = _passing_artifact()
    sec = a["candidate_research"]["inference"][name]
    longest = max(sec["block_intervals"], key=lambda iv: iv["block_ms"])
    longest["residual_dependence"] = report
    return a, longest


class InfluenceScoreAuthority(unittest.TestCase):
    def test_1_mean_aggregates_reproduce_approved_expectancy(self):
        rows = _noise_rows()
        out = inf.dependence_aware_mean(rows, APPROVED, samples=100)
        self.assertEqual(out["residual_series_kind"], "MEAN_INFLUENCE_SCORE_V1")
        for iv in out["block_intervals"]:
            rd = iv["residual_dependence"]
            if rd is None:
                continue
            self.assertEqual(rd["residual_series_kind"], "MEAN_INFLUENCE_SCORE_V1")
            _, _, point = inf.influence_scores(rd["series"], diff=False)
            self.assertAlmostEqual(point, out["mean_r"], places=9)
            self.assertEqual(sum(rd["series"]["count"]), out["n"])

    def test_2_difference_aggregates_reproduce_uplift(self):
        rows = _noise_rows()
        out = inf.dependence_aware_diff(rows, APPROVED, BASELINE, samples=100)
        self.assertEqual(out["residual_series_kind"], "DIFF_MEAN_INFLUENCE_SCORE_V1")
        for iv in out["block_intervals"]:
            rd = iv["residual_dependence"]
            if rd is None:
                continue
            _, _, point = inf.influence_scores(rd["series"], diff=True)
            self.assertAlmostEqual(point, out["delta"], places=9)

    def test_3_variable_counts_keep_the_estimator_count_weighted(self):
        series = {"block_ids": [0, 1, 2, 3], "sum_r": [1.0, 30.0, 2.0, 60.0], "count": [1, 10, 2, 20]}
        ids, psi, point = inf.influence_scores(series, diff=False)
        self.assertAlmostEqual(point, 93.0 / 33.0)                         # count-weighted
        self.assertNotAlmostEqual(point, (1.0 + 3.0 + 1.0 + 3.0) / 4)      # not the block-mean average
        mean_count = 33 / 4
        for (s_, c), p in zip(zip(series["sum_r"], series["count"]), psi):
            self.assertAlmostEqual(p, (s_ - point * c) / mean_count)
        self.assertAlmostEqual(sum(p * mean_count for p in psi), 0.0)       # influence sums to zero

    def test_4_nested_counterexample_old_rule_clean_new_rule_fails(self):
        rows = _counterexample_rows()
        old = inf.paired_block_acf(rows, APPROVED, BASELINE, DAY, min_blocks=30)
        new = inf.influence_residual(rows, APPROVED, BASELINE, DAY)
        self.assertTrue(old["estimable"])
        self.assertFalse(old["significant"])            # equal-weight paired delta looks clean
        self.assertTrue(new["significant"])             # estimator-aligned influence does not
        out = inf.dependence_aware_diff(rows, APPROVED, BASELINE, samples=100)
        self.assertEqual(out["authority_status"], "UPLIFT_RESIDUAL_DEPENDENCE")
        self.assertIsNone(out["authority_ci_low"])
        a = _passing_artifact()
        a["candidate_research"]["inference"]["uplift_vs_baseline"] = copy.deepcopy(out)
        self.assertIn("UPLIFT_RESIDUAL_DEPENDENCE", gate.evaluate(a).blockers)

    def test_5_zero_a_blocks_remain_in_the_uplift_timeline(self):
        rows = [r for r in _noise_rows() if r["approved"] is False or (r["ts"] - D0) // DAY % 4 != 0]
        rd = inf.influence_residual(rows, APPROVED, BASELINE, DAY)
        s = rd["series"]
        self.assertEqual(len(s["block_ids"]), 120)                       # baseline defines the timeline
        zero_a = [i for i, c in enumerate(s["a_count"]) if c == 0]
        self.assertEqual(len(zero_a), 30)
        ids, psi, _ = inf.influence_scores(s, diff=True)
        self.assertEqual(ids, s["block_ids"])
        mu_b = sum(s["b_sum"]) / sum(s["b_count"])
        mc_b = sum(s["b_count"]) / len(ids)
        for i in zero_a:                                                  # psi = 0 - psi_B there
            self.assertAlmostEqual(psi[i], -(s["b_sum"][i] - mu_b * s["b_count"][i]) / mc_b)
        # And the gate accepts zero-A blocks (count 0 with sum 0) as valid aggregates.
        a, _ = _section("uplift_vs_baseline",
                        paired_residual(clean_series(45), a_counts=[0 if i % 5 == 0 else 3 for i in range(45)]))
        a["candidate_research"]["inference"]["uplift_vs_baseline"]["block_intervals"][-1]["resampling_blocks"] = 36
        self.assertNotIn("UPLIFT_RESIDUAL_SERIES_INCONSISTENT", gate.evaluate(a).blockers)

    def test_6_calendar_gaps_break_adjacency(self):
        for name, rep in (("approved_expectancy", residual(clean_series(45), block_ids=list(range(0, 90, 2)))),
                          ("uplift_vs_baseline", paired_residual(clean_series(45), block_ids=list(range(0, 90, 2))))):
            a, _ = _section(name, rep)
            code = "UPLIFT_RESIDUAL_DEPENDENCE_NOT_ESTIMABLE" if name.startswith("uplift") else \
                "RESIDUAL_DEPENDENCE_NOT_ESTIMABLE"
            self.assertIn(code, gate.evaluate(a).blockers)
        res = inf.acf_from_block_series([100, 101, 105], [0.1, 0.2, 0.3], min_pairs=1)
        self.assertEqual(res["lags"]["1"]["eligible_pairs"], 1)

    def test_7_tampered_aggregates_are_detected(self):
        a, iv = _section("uplift_vs_baseline", paired_residual(clean_series(45)))
        iv["residual_dependence"]["series"]["a_sum"][4] += 3.0            # moves the point estimate
        self.assertIn("RESIDUAL_AGGREGATES_POINT_ESTIMATE_MISMATCH", gate.evaluate(a).blockers)
        a, iv = _section("uplift_vs_baseline", paired_residual(clean_series(45)))
        s = iv["residual_dependence"]["series"]
        s["b_sum"][4] += 2.0
        s["b_sum"][30] -= 2.0                                             # totals unchanged, ACF changes
        self.assertIn("UPLIFT_RESIDUAL_SERIES_INCONSISTENT", gate.evaluate(a).blockers)
        a, iv = _section("approved_expectancy", residual(clean_series(45)))
        iv["residual_dependence"]["series"]["count"][2] += 1               # changes mu
        self.assertIn("RESIDUAL_AGGREGATES_POINT_ESTIMATE_MISMATCH", gate.evaluate(a).blockers)
        a, iv = _section("approved_expectancy", residual(clean_series(45)))
        iv["residual_dependence"]["psi"] = [0.0] * 45                      # a stored score array is ignored
        iv["residual_dependence"]["acf"] = {"1": 0.0, "2": 0.0, "3": 0.0}
        self.assertIn("RESIDUAL_DEPENDENCE_SERIES_INCONSISTENT", gate.evaluate(a).blockers)
        for kind in ("PAIRED_BLOCK_DELTA", "BLOCK_MEAN", None):          # legacy series have no authority
            a, iv = _section("uplift_vs_baseline", paired_residual(clean_series(45)))
            iv["residual_dependence"]["residual_series_kind"] = kind
            self.assertIn("UPLIFT_RESIDUAL_SERIES_KIND_INVALID", gate.evaluate(a).blockers)
        a, _ = _section("approved_expectancy", residual(clean_series(45)))
        a["candidate_research"]["inference"]["approved_expectancy"]["mean_r"] = MEAN_TARGET + 0.01
        self.assertIn("RESIDUAL_AGGREGATES_POINT_ESTIMATE_MISMATCH", gate.evaluate(a).blockers)
        a, _ = _section("uplift_vs_baseline", paired_residual(clean_series(45)))
        a["candidate_research"]["inference"]["uplift_vs_baseline"]["delta"] = DIFF_TARGET - 0.01
        self.assertIn("RESIDUAL_AGGREGATES_POINT_ESTIMATE_MISMATCH", gate.evaluate(a).blockers)

    def test_8_constant_influence_is_not_estimable(self):
        a, _ = _section("approved_expectancy", residual([0.0] * 45))
        self.assertIn("RESIDUAL_DEPENDENCE_NOT_ESTIMABLE", gate.evaluate(a).blockers)
        a, _ = _section("uplift_vs_baseline", paired_residual([0.0] * 45))
        self.assertIn("UPLIFT_RESIDUAL_DEPENDENCE_NOT_ESTIMABLE", gate.evaluate(a).blockers)
        rows = [_row(D0 + d * DAY + k * HOUR, True, 0.2) for d in range(120) for k in range(3)]
        out = inf.dependence_aware_mean(rows, APPROVED, samples=100)
        self.assertEqual(out["authority_status"], inf.RESIDUAL_NOT_ESTIMABLE)
        self.assertIsNone(out["authority_ci_low"])

    def test_9_clean_influence_series_keeps_authority(self):
        rows = _noise_rows()
        appr = inf.dependence_aware_mean(rows, APPROVED, samples=200)
        up = inf.dependence_aware_diff(rows, APPROVED, BASELINE, samples=200)
        self.assertEqual(appr["authority_status"], inf.AUTHORITY_VALID,
                         appr["residual_dependence_at_longest_usable"]["acf"])
        self.assertEqual(up["authority_status"], inf.AUTHORITY_VALID,
                         up["residual_dependence_at_longest_usable"]["acf"])
        self.assertEqual(gate.evaluate(_passing_artifact()).blockers, [])


if __name__ == "__main__":
    unittest.main()
