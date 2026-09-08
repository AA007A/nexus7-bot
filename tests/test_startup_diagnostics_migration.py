import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from bot import startup_block


class StartupDiagnosticsMigrationTests(unittest.TestCase):
    def setUp(self):
        startup_block._startup_diagnostics_done = False

    def tearDown(self):
        startup_block._startup_diagnostics_done = False

    def test_diagnostic_runs_once_from_classifier(self):
        calls = []
        fake = types.SimpleNamespace(
            audit_silent_excepts=lambda log: calls.append(log) or []
        )
        with patch.dict(sys.modules, {"bot.silent_except_audit": fake}):
            first = startup_block.classify_startup_block(
                sitecustomize_status="ok", critical_issues=(), selfcheck_error=None
            )
            second = startup_block.classify_startup_block(
                sitecustomize_status="ok", critical_issues=(), selfcheck_error=None
            )
        self.assertIsNone(first)
        self.assertIsNone(second)
        self.assertEqual(len(calls), 1)

    def test_diagnostic_failure_does_not_change_fail_closed_classification(self):
        def failing_audit(log):
            raise RuntimeError("diagnostic failure")

        fake = types.SimpleNamespace(audit_silent_excepts=failing_audit)
        with patch.dict(sys.modules, {"bot.silent_except_audit": fake}):
            block = startup_block.classify_startup_block(
                sitecustomize_status="failed", critical_issues=(), selfcheck_error=None
            )
        self.assertIsNotNone(block)
        self.assertEqual(block.code, "SITECUSTOMIZE_NOT_CONFIRMED")

    def test_sitecustomize_no_longer_invokes_silent_except_audit(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / "sitecustomize.py").read_text(encoding="utf-8")
        self.assertNotIn("silent_except_audit", source)
        self.assertNotIn("audit_silent_excepts", source)


if __name__ == "__main__":
    unittest.main()
