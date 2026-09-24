"""The PR promotion gate must exit non-zero on any unmet condition."""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from bot import nexus_oos_promotion_gate as gate


def _passing_artifact():
    return {
        "status": "AI_EDGE_PROVEN",
        "blockers": [],
        "historical_context_parity_complete": True,
        "report": {
            "known_baseline_outcomes": 2000,
            "known_approved_outcomes": 400,
            "bootstrap_ci_low_r": 0.05,
            "nexus_expectancy_r": 0.20,
        },
        "performance": {"approved": {"expectancy_ci_low_r": 0.04}},
        "symbols": [
            {"symbol": s, "error": None, "history_days": 90.0,
             "historical_context": {"parity_complete": True}}
            for s in ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT")
        ],
        "concentration": {
            "approved_by_symbol": {"groups_positive": 5, "top_share_of_positive_r": 0.3},
            "approved_by_month": {"groups_positive": 3, "top_share_of_positive_r": 0.4},
        },
        "robustness": {"summary": {"temporal_folds_positive_uplift": 4}},
        "cost_stress_approved": {
            "fees_plus_50pct": {"net_expectancy_r": 0.1},
            "slippage_x2": {"net_expectancy_r": 0.08},
        },
        "methodology": {"closed_candles_only": True, "historical_clock_frozen": True,
                        "fees_included": True, "slippage_included": True},
    }


# Latest real CI evidence (run 35467406970): uplift positive, both negative.
SEPT19 = {
    "status": "AI_EDGE_NOT_PROVEN",
    "blockers": ["HISTORICAL_CONTEXT_PARITY_INCOMPLETE", "UPLIFT_NOT_STATISTICALLY_POSITIVE"],
    "report": {"known_baseline_outcomes": 803, "known_approved_outcomes": 126,
               "bootstrap_ci_low_r": -0.043, "nexus_expectancy_r": -0.526,
               "baseline_expectancy_r": -0.703, "expectancy_uplift_r": 0.177},
    "symbols": [{"symbol": "BTCUSDT", "historical_context": {"parity_complete": False}}],
    "methodology": {"closed_candles_only": True, "historical_clock_frozen": True,
                    "fees_included": True, "slippage_included": True},
}


class PromotionGateTests(unittest.TestCase):
    def _run(self, data=None, *, raw=None, missing=False, extra=()):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.json"
            if not missing:
                path.write_text(raw if raw is not None else json.dumps(data), encoding="utf-8")
            return gate.main([str(path), *extra])

    def test_fully_proven_artifact_exits_zero(self):
        self.assertEqual(self._run(_passing_artifact()), gate.EXIT_PROMOTE)

    def test_ai_edge_not_proven_exits_non_zero(self):
        a = _passing_artifact()
        a["status"] = "AI_EDGE_NOT_PROVEN"
        self.assertNotEqual(self._run(a), 0)

    def test_missing_artifact_exits_non_zero(self):
        self.assertEqual(self._run(missing=True), gate.EXIT_MISSING)

    def test_corrupt_artifact_exits_non_zero(self):
        self.assertEqual(self._run(raw="{not json"), gate.EXIT_CORRUPT)
        self.assertEqual(self._run(raw="[]"), gate.EXIT_CORRUPT)
        self.assertEqual(self._run({"status": "AI_EDGE_PROVEN"}), gate.EXIT_CORRUPT)

    def test_incomplete_parity_blocks_production_promotion(self):
        a = _passing_artifact()
        a["historical_context_parity_complete"] = False
        self.assertNotEqual(self._run(a), 0)
        r = gate.evaluate(a)
        self.assertIn("HISTORICAL_CONTEXT_PARITY_INCOMPLETE", r.blockers)
        # Research-only flag relaxes exactly that condition.
        self.assertEqual(self._run(a, extra=("--allow-incomplete-context-parity",)), 0)

    def test_uplift_lower_ci_not_positive_blocks(self):
        for lo in (0.0, -0.01, None):
            a = _passing_artifact()
            a["report"]["bootstrap_ci_low_r"] = lo
            self.assertIn("UPLIFT_CI_NOT_POSITIVE", gate.evaluate(a).blockers)
            self.assertNotEqual(self._run(a), 0)

    def test_negative_approved_expectancy_blocks_even_with_positive_uplift(self):
        a = _passing_artifact()
        a["report"]["nexus_expectancy_r"] = -0.10
        a["report"]["bootstrap_ci_low_r"] = 0.2
        r = gate.evaluate(a)
        self.assertIn("APPROVED_EXPECTANCY_NOT_POSITIVE", r.blockers)
        self.assertNotEqual(self._run(a), 0)

    def test_approved_expectancy_ci_must_be_positive(self):
        a = _passing_artifact()
        a["performance"]["approved"]["expectancy_ci_low_r"] = -0.01
        self.assertIn("APPROVED_EXPECTANCY_CI_NOT_POSITIVE", gate.evaluate(a).blockers)

    def test_insufficient_sample_blocks(self):
        a = _passing_artifact()
        a["report"]["known_approved_outcomes"] = 30
        a["report"]["known_baseline_outcomes"] = 50
        r = gate.evaluate(a)
        self.assertIn("INSUFFICIENT_APPROVED_SAMPLE", r.blockers)
        self.assertIn("INSUFFICIENT_BASELINE_SAMPLE", r.blockers)
        self.assertNotEqual(self._run(a), 0)

    def test_concentration_breadth_horizon_and_cost_stress_block(self):
        cases = {
            "SINGLE_SYMBOL_DOMINATES": lambda a: a["concentration"]["approved_by_symbol"].update(top_share_of_positive_r=0.8),
            "TOO_FEW_SYMBOLS_CONTRIBUTING": lambda a: a["concentration"]["approved_by_symbol"].update(groups_positive=1),
            "SINGLE_PERIOD_DOMINATES": lambda a: a["concentration"]["approved_by_month"].update(top_share_of_positive_r=0.9),
            "HISTORY_HORIZON_TOO_SHORT": lambda a: a["symbols"][0].update(history_days=26.0),
            "SYMBOLS_UNAVAILABLE": lambda a: a["symbols"][0].update(error="insufficient_history"),
            "COST_STRESS_FAILS_SLIPPAGE_X2": lambda a: a["cost_stress_approved"]["slippage_x2"].update(net_expectancy_r=-0.01),
            "TEMPORAL_ROBUSTNESS_INSUFFICIENT": lambda a: a["robustness"]["summary"].update(temporal_folds_positive_uplift=1),
        }
        for blocker, mutate in cases.items():
            a = copy.deepcopy(_passing_artifact())
            mutate(a)
            with self.subTest(blocker=blocker):
                self.assertIn(blocker, gate.evaluate(a).blockers)

    def test_missing_evidence_sections_fail_closed(self):
        for key in ("performance", "concentration", "robustness", "cost_stress_approved", "methodology"):
            a = _passing_artifact()
            del a[key]
            with self.subTest(key=key):
                self.assertFalse(gate.evaluate(a).promote)

    def test_latest_real_ci_evidence_is_blocked(self):
        r = gate.evaluate(copy.deepcopy(SEPT19))
        self.assertFalse(r.promote)
        for blocker in ("STATUS_NOT_AI_EDGE_PROVEN", "APPROVED_EXPECTANCY_NOT_POSITIVE",
                        "UPLIFT_CI_NOT_POSITIVE", "HISTORICAL_CONTEXT_PARITY_INCOMPLETE"):
            self.assertIn(blocker, r.blockers)


if __name__ == "__main__":
    unittest.main()
