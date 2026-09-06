"""Startup must prove that all mandatory sitecustomize hardenings installed."""
import ast
import builtins
from pathlib import Path
import unittest

from bot.startup_block import classify_startup_block


ROOT = Path(__file__).resolve().parents[1]


class SitecustomizeFailClosedTests(unittest.TestCase):
    def test_current_hardening_installation_sets_ok_marker(self):
        import sitecustomize  # noqa: F401 - installation is the behavior under test

        self.assertEqual(
            getattr(builtins, "_nexus_sitecustomize_status", None), "ok"
        )

    def test_main_routes_marker_into_fail_closed_classifier(self):
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        self.assertIn('"_nexus_sitecustomize_status", "not_loaded"', source)
        self.assertIn("sitecustomize_status=_sitecustomize_status", source)
        self.assertIn("_blocked_by_selfcheck = _startup_block is not None", source)
        self.assertTrue(any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "critical"
            for node in ast.walk(tree)
        ))

        block = classify_startup_block(sitecustomize_status="not_loaded")
        self.assertIsNotNone(block)
        self.assertEqual(block.code, "SITECUSTOMIZE_NOT_CONFIRMED")

    def test_sitecustomize_failure_is_not_silent(self):
        source = (ROOT / "sitecustomize.py").read_text(encoding="utf-8")
        self.assertIn('builtins._nexus_sitecustomize_status = "failed"', source)
        self.assertNotIn("except Exception:\n    pass", source)


if __name__ == "__main__":
    unittest.main()
