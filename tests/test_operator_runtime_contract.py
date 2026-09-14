import os
import subprocess
import sys
import unittest


class OperatorRuntimeContractTests(unittest.TestCase):
    def test_live_operator_contract_remains_50x_and_50pct(self):
        """Regression guard: hardening work must not silently change operator sizing.

        The production environment owns LEVERAGE, while the operator runtime policy
        owns the controlled-LIVE margin fraction. Exercise both in a clean child
        interpreter so this test cannot inherit module-level config cached by the
        rest of the suite.
        """
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
            cwd=os.path.dirname(os.path.dirname(__file__)),
            env=env,
            text=True,
            capture_output=True,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("operator contract ok", proc.stdout)


if __name__ == "__main__":
    unittest.main()
