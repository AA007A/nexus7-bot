"""Residual dependence of a DIFFERENCE statistic uses its own paired block series.

1. The uplift residual series differs from the approved-expectancy series when
   the baseline's block behaviour differs (identical approved block means).
2. Persistent serial dependence in the paired block uplift removes uplift
   authority even when the approved-series ACF is clean.
3. A clean paired uplift series keeps authority.
4. Missing A or B population in too many blocks fails closed.
5. A tampered paired aggregate series is rejected by the gate.
6. Constant approved block means are NOT_ESTIMABLE, not "non-significant".
7. A constant uplift delta is NOT_ESTIMABLE.
8. Calendar gaps still break lag adjacency (paired series too).
"""
import copy
import math
import random
import unittest

from bot import nexus_oos_inference as inf
from bot import nexus_oos_promotion_gate as gate
from tests.test_nexus_oos_promotion_gate import (_passing_artifact, clean_series, paired_residual,
                                                  residual)

DAY = inf.DAY_MS
HOUR = 3_600_000
D0 = 1_735_689_600_000
APPROVED = lambda r: r["approved"]  # noqa: E731
BASELINE = lambda r: True  # noqa: E731


def _rows(baseline_level, *, days=120, seed=11):
    """Approved rows identical across datasets; non-approved rows follow ``baseline_level(d)``."""
    rng_a, rng_b = random.Random(seed), random.Random(seed + 1)
    rows = []
    for d in range(days):
        a_val = rng_a.gauss(0.2, 0.4)
        for k in range(2):                                    # approved: identical in every dataset
            ts = D0 + d * DAY + k * 6 * HOUR
            rows.append({"ts": ts, "outcome_end_ts": ts + 3 * HOUR, "approved": True,
                         "r": a_val + (0.01 if k else -0.01), "outcome_status": "RESOLVED"})
        for k in range(6):                                    # non-approved: dataset-specific
            ts = D0 + d * DAY + (k + 2) * 2 * HOUR
            rows.append({"ts": ts, "outcome_end_ts": ts + 3 * HOUR, "approved": False,
                         "r": baseline_level(d) + rng_b.gauss(0, 0.05), "outcome_status": "RESOLVED"})
    return rows


NOISE = lambda d: random.Random(1000 + d).gauss(0.0, 0.6)       # noqa: E731
REGIME = lambda d: 1.5 * math.sin(2 * math.pi * d / 45.0)       # noqa: E731


def _uplift_section(**kw):
    a = _passing_artifact()
    sec = a["candidate_research"]["inference"]["uplift_vs_baseline"]
    longest = max(sec["block_intervals"], key=lambda iv: iv["block_ms"])
    longest["residual_dependence"] = paired_residual(**kw)
    return a, longest


class PairedResidualSeries(unittest.TestCase):
    def test_1_uplift_series_differs_when_only_the_baseline_differs(self):
        rx, ry = _rows(NOISE), _rows(REGIME)
        for ms in (DAY, 3 * DAY):
            ax, ay = inf.block_acf(rx, APPROVED, ms), inf.block_acf(ry, APPROVED, ms)
            self.assertEqual(ax["series"], ay["series"])          # identical approved block means
            self.assertEqual(ax["acf"], ay["acf"])
            px = inf.paired_block_acf(rx, APPROVED, BASELINE, ms)
            py = inf.paired_block_acf(ry, APPROVED, BASELINE, ms)
            self.assertEqual(px["residual_series_kind"], "PAIRED_BLOCK_DELTA")
            self.assertEqual(px["series"]["a_sum"], py["series"]["a_sum"])
            self.assertNotEqual(px["series"]["b_sum"], py["series"]["b_sum"])
            self.assertNotEqual(px["acf"], py["acf"])
            self.assertNotEqual(px["acf"], ax["acf"])             # never the approved-only series
        self.assertTrue(inf.paired_block_acf(ry, APPROVED, BASELINE, 3 * DAY)["significant"])

    def test_2_persistent_paired_dependence_removes_uplift_authority(self):
        ry = _rows(REGIME)
        appr = inf.dependence_aware_mean(ry, APPROVED, samples=200)
        self.assertEqual(appr["authority_status"], inf.AUTHORITY_VALID)   # approved ACF is clean
        up = inf.dependence_aware_diff(ry, APPROVED, BASELINE, samples=200)
        self.assertEqual(up["residual_series_kind"], "PAIRED_BLOCK_DELTA")
        self.assertEqual(up["authority_status"], inf.PAIRED_RESIDUAL_DEPENDENCE)
        self.assertIsNone(up["authority_ci_low"])
        # The gate reaches the same verdict from the artifact aggregates alone.
        a = _passing_artifact()
        a["candidate_research"]["inference"]["uplift_vs_baseline"] = copy.deepcopy(up)
        a["candidate_research"]["inference"]["required_block_ms"] = up["required_block_ms"]
        r = gate.evaluate(a)
        self.assertIn("UPLIFT_RESIDUAL_DEPENDENCE", r.blockers)
        self.assertIn("UPLIFT_BLOCK_CI_NOT_POSITIVE", r.blockers)

    def test_3_clean_paired_series_keeps_authority(self):
        rx = _rows(NOISE, seed=12)            # an i.i.d. draw whose paired ACF is inside the band
        up = inf.dependence_aware_diff(rx, APPROVED, BASELINE, samples=200)
        self.assertEqual(up["authority_status"], inf.AUTHORITY_VALID, up["residual_dependence_at_longest_usable"])
        self.assertIsNotNone(up["authority_ci_low"])
        a, _ = _uplift_section(a_means=clean_series(45))
        self.assertEqual(gate.evaluate(a).blockers, [])

    def test_4_missing_a_or_b_in_too_many_blocks_fails_closed(self):
        m = clean_series(45)
        b_counts = [0 if i % 3 == 0 else 5 for i in range(45)]         # B absent in 15 of 45 blocks
        a, _ = _uplift_section(a_means=m, b_counts=b_counts)
        blockers = gate.evaluate(a).blockers
        self.assertIn("UPLIFT_RESIDUAL_DEPENDENCE_NOT_ESTIMABLE", blockers)
        self.assertIn("UPLIFT_BLOCK_CI_NOT_POSITIVE", blockers)
        rows = [r for r in _rows(NOISE) if r["approved"] or (r["ts"] - D0) // DAY % 3 == 0]
        res = inf.paired_block_acf(rows, APPROVED, lambda r: not r["approved"], DAY)
        self.assertFalse(res["estimable"])                             # no lag-1 calendar pairs

    def test_5_tampered_paired_aggregates_are_rejected(self):
        a, iv = _uplift_section(a_means=clean_series(45))
        iv["residual_dependence"]["series"]["b_sum"][7] += 4.0           # stored ACF no longer matches
        self.assertIn("UPLIFT_RESIDUAL_SERIES_INCONSISTENT", gate.evaluate(a).blockers)
        for mutate, code in (
                (lambda rd: rd.update(residual_series_kind="APPROVED_BLOCK_MEAN"), "UPLIFT_RESIDUAL_SERIES_KIND_INVALID"),
                (lambda rd: rd.pop("residual_series_kind"), "UPLIFT_RESIDUAL_SERIES_KIND_INVALID"),
                (lambda rd: rd["series"]["b_count"].__setitem__(3, -1), "UPLIFT_RESIDUAL_SERIES_INCONSISTENT"),
                (lambda rd: rd["series"]["a_count"].__setitem__(3, 0), "UPLIFT_RESIDUAL_SERIES_INCONSISTENT"),
                (lambda rd: rd["series"].pop("b_sum"), "UPLIFT_RESIDUAL_SERIES_MISSING")):
            a, iv = _uplift_section(a_means=clean_series(45))
            mutate(iv["residual_dependence"])
            self.assertIn(code, gate.evaluate(a).blockers)
        # An approved-only (single-mean) report can never authorize uplift.
        a, iv = _uplift_section(a_means=clean_series(45))
        iv["residual_dependence"] = residual(clean_series(45))
        self.assertIn("UPLIFT_RESIDUAL_SERIES_KIND_INVALID", gate.evaluate(a).blockers)

    def test_6_constant_approved_block_means_are_not_estimable(self):
        res = inf.acf_from_block_series(list(range(45)), [0.2] * 45)
        self.assertFalse(res["estimable"])
        self.assertIsNone(res["significant"])
        self.assertEqual(res["not_estimable_reason"], "ZERO_VARIANCE")
        a = _passing_artifact()
        sec = a["candidate_research"]["inference"]["approved_expectancy"]
        longest = max(sec["block_intervals"], key=lambda iv: iv["block_ms"])
        longest["residual_dependence"] = residual([0.2] * longest["resampling_blocks"])
        blockers = gate.evaluate(a).blockers
        self.assertIn("RESIDUAL_DEPENDENCE_NOT_ESTIMABLE", blockers)
        self.assertIn("APPROVED_EXPECTANCY_BLOCK_CI_NOT_POSITIVE", blockers)
        rows = [{"ts": D0 + d * DAY + k * HOUR, "outcome_end_ts": D0 + d * DAY + k * HOUR + HOUR,
                 "approved": True, "r": 0.2, "outcome_status": "RESOLVED"} for d in range(120) for k in range(3)]
        out = inf.dependence_aware_mean(rows, APPROVED, samples=100)
        self.assertEqual(out["authority_status"], inf.RESIDUAL_NOT_ESTIMABLE)
        self.assertIsNone(out["authority_ci_low"])

    def test_7_constant_uplift_delta_is_not_estimable(self):
        a, _ = _uplift_section(a_means=[0.2] * 45)
        self.assertIn("UPLIFT_RESIDUAL_DEPENDENCE_NOT_ESTIMABLE", gate.evaluate(a).blockers)

    def test_8_calendar_gaps_break_paired_adjacency(self):
        a, _ = _uplift_section(a_means=clean_series(45), block_ids=list(range(0, 90, 2)))
        self.assertIn("UPLIFT_RESIDUAL_DEPENDENCE_NOT_ESTIMABLE", gate.evaluate(a).blockers)
        series = {"block_ids": [100, 101, 105], "a_sum": [1.0, 2.0, 3.0], "a_count": [1, 1, 1],
                  "b_sum": [0.0, 0.0, 0.0], "b_count": [1, 1, 1]}
        ids, deltas = inf.paired_delta_from_aggregates(series)
        res = inf.acf_from_block_series(ids, deltas, min_pairs=1)
        self.assertEqual(res["lags"]["1"]["eligible_pairs"], 1)            # 100->101 only
        self.assertEqual(res["lags"]["2"]["eligible_pairs"], 0)


if __name__ == "__main__":
    unittest.main()
