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
