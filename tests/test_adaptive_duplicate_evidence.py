import unittest

from bot import adaptive_mtf_calibration as cal


class AdaptiveDuplicateEvidenceTests(unittest.TestCase):
    def test_four_score_failures_collapse_to_one_family(self):
        a = cal.evidence_attribution([
            "SCORE_4H", "SCORE_1H", "SCORE_15M", "SCORE_COMBINED"
        ])
        self.assertEqual(a["raw_count"], 4)
        self.assertEqual(a["independent_count"], 1)
        self.assertEqual(a["collapsed_duplicates"], 3)
        self.assertEqual(a["families"], ["SCORE_FAMILY"])

    def test_score_overlap_checks_are_explicit(self):
        a = cal.evidence_attribution([
            "SCORE_COMBINED", "ALIGN_15M", "ADX", "VOLUME",
            "ENTRY_TYPE", "EXTENSION"
        ])
        self.assertIn("ALIGN_15M", a["score_tf_overlap_checks"])
        self.assertIn("ADX", a["score_tf_overlap_checks"])
        self.assertIn("VOLUME", a["score_tf_overlap_checks"])
        self.assertNotIn("ENTRY_TYPE", a["score_tf_overlap_checks"])
        self.assertNotIn("EXTENSION", a["score_tf_overlap_checks"])
        self.assertIn("SCORE_FAMILY", a["nexus_recheck_families"])
        self.assertIn("TREND_ALIGNMENT", a["nexus_recheck_families"])
        self.assertIn("TREND_STRENGTH", a["nexus_recheck_families"])
        self.assertIn("ACTIVITY_VOLUME", a["nexus_recheck_families"])

    def test_entry_and_extension_remain_independent(self):
        a = cal.evidence_attribution(["ENTRY_TYPE", "EXTENSION"])
        self.assertEqual(a["raw_count"], 2)
        self.assertEqual(a["independent_count"], 2)
        self.assertEqual(a["collapsed_duplicates"], 0)
        self.assertEqual(a["families"], ["ENTRY_TRIGGER", "ANTI_CHASE"])

    def test_failure_vector_preserves_decision_semantics(self):
        thresholds = {
            "min_4h": 60,
            "min_1h": 65,
            "min_15m": 80,
            "min_combined": 72,
            "min_vol": 1.2,
            "min_adx": 18,
            "max_extension_atr": 2.5,
        }
        base = {
            "ok": True,
            "total": 50,
            "vol_r": 1.0,
            "adx_v": 15,
            "rsi_v": 50,
            "aligned": False,
            "trend_s": 12,
            "vol_s": 7,
            "momentum_s": 12,
            "atr_s": 10,
            "struct_s": 9,
        }
        snap = cal.failure_vector(
            direction="LONG",
            bull_4h=False,
            bear_4h=False,
            bull_1h=False,
            bear_1h=False,
            s4h=dict(base),
            s1h=dict(base),
            s15=dict(base),
            combined=50,
            entry_type="MOMENTUM",
            extension_atr=0.5,
            thresholds=thresholds,
        )
        self.assertEqual(
            snap["failures"],
            ["SCORE_4H", "SCORE_1H", "SCORE_15M", "SCORE_COMBINED",
             "ALIGN_15M", "VOLUME", "ADX"],
        )
        self.assertEqual(snap["failure_count"], 7)
        # Attribution is additive telemetry only; raw failures are unchanged.
        self.assertEqual(snap["attribution"]["raw_count"], 7)
        self.assertEqual(snap["attribution"]["independent_count"], 4)
        self.assertEqual(snap["attribution"]["collapsed_duplicates"], 3)
        self.assertEqual(snap["components_15m"]["trend"], 18.0)
        self.assertEqual(snap["components_15m"]["volume"], 13.0)


if __name__ == "__main__":
    unittest.main()
