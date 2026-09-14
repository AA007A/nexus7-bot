import os
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class OperatorRuntimeContractTests(unittest.TestCase):
    def test_live_operator_contract_remains_50x_and_50pct(self):
        """Hardening must not silently change the requested LIVE sizing contract."""
        env = dict(os.environ)
        env["LEVERAGE"] = "50"
        code = (
            "from bot.config import cfg; "
            "from bot.operator_runtime_policy import MARGIN_FRACTION; "
            "assert cfg.LEVERAGE == 50, cfg.LEVERAGE; "
            "assert MARGIN_FRACTION == 0.50, MARGIN_FRACTION; "
            "print('operator contract ok')"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("operator contract ok", proc.stdout)

    def test_drawdown_remains_advisory_for_new_entries(self):
        source = (ROOT / "bot" / "operator_runtime_policy.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("entries_blocked=false", source)
        self.assertIn("active_restored=true", source)
        self.assertIn("drawdown=advisory_only", source)


if __name__ == "__main__":
    unittest.main()
