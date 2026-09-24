"""The OOS replay workflow must trigger on every decision-affecting path."""
import fnmatch
import re
import unittest
from pathlib import Path

from bot.decision_affecting_paths import DECISION_AFFECTING_PATHS

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "oos_real_replay.yml"


def _workflow_paths():
    text = WORKFLOW.read_text(encoding="utf-8")
    block = text[text.index("    paths:\n"):text.index("  workflow_dispatch:")]
    return set(re.findall(r'^\s+- "([^"]+)"\s*$', block, flags=re.M))


class DecisionAffectingPathsTests(unittest.TestCase):
    def test_workflow_filter_contains_every_decision_path(self):
        missing = sorted(set(DECISION_AFFECTING_PATHS) - _workflow_paths())
        self.assertEqual(missing, [], f"oos_real_replay.yml is missing: {missing}")

    def test_every_pattern_matches_an_existing_file(self):
        files = [str(p.relative_to(ROOT)) for p in (ROOT / "bot").glob("*.py")]
        for pattern in DECISION_AFFECTING_PATHS:
            self.assertTrue(any(fnmatch.fnmatch(f, pattern) for f in files), pattern)

    def test_core_decision_modules_are_covered(self):
        required = (
            "bot/nexus_ai.py", "bot/nexus_models.py", "bot/nexus_probability.py",
            "bot/strategy.py", "bot/score.py", "bot/indicators.py", "bot/config.py",
            "bot/risk_policy.py", "bot/final_sizing_invariants.py",
            "bot/kucoin_execution_model.py", "bot/market_data.py",
        )
        for path in required:
            self.assertTrue(
                any(fnmatch.fnmatch(path, p) for p in DECISION_AFFECTING_PATHS), path
            )


if __name__ == "__main__":
    unittest.main()


class PromotionGateWorkflowContract(unittest.TestCase):
    def test_pr_workflow_runs_strict_gate_last(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("python -m bot.nexus_oos_promotion_gate artifacts/nexus_oos_real_replay.json --gate research", text)
        gate_idx = text.index("Research promotion gate (strict)")
        self.assertGreater(gate_idx, text.index("Upload replay evidence"))
        # The strict research gate is the last step; the live gate is informational.
        self.assertGreater(gate_idx, text.index("Live release gate (informational, stage C)"))
        block = text[gate_idx:]
        self.assertNotIn("- name:", block[len("Research promotion gate (strict)"):])
        # Never allowed to pass through for pull requests.
        self.assertIn("continue-on-error: ${{ github.event_name == 'workflow_dispatch' &&", block)
        self.assertIn('exit "${PIPESTATUS[0]}"', block)
        self.assertNotIn("--allow-incomplete-context-parity", text)

    def test_replay_uses_pinned_manifest_and_parity_tests(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("--policy-manifest research/replay_policy_manifest.json", text)
        self.assertIn('"research/replay_policy_manifest.json"', text)
        self.assertIn("python -m unittest tests.test_nexus_oos_execution_parity -v", text)
        self.assertIn("python -m unittest tests.test_nexus_oos_censoring_and_horizon -v", text)
        self.assertIn("python -m unittest tests.test_nexus_oos_fold_independence_and_gates -v", text)
        self.assertIn("python -m unittest tests.test_live_release_evidence -v", text)
        self.assertIn("python -m unittest tests.test_nexus_oos_influence_residual -v", text)
        self.assertIn("python -m unittest tests.test_ai_decision_authority -v", text)
        # LIVE evidence is never read from a committed file.
        self.assertNotIn("live_policy_attestation.txt", text)
        self.assertNotIn("--policy-observation-fixture", text)
        self.assertTrue((ROOT / "research" / "replay_policy_manifest.json").is_file())

    def test_replay_covers_production_universe(self):
        from bot.config import cfg
        text = WORKFLOW.read_text(encoding="utf-8")
        line = next(l for l in text.splitlines() if "OOS_SYMBOLS:" in l)
        for symbol in cfg.SYMBOLS:
            self.assertIn(symbol, line)
