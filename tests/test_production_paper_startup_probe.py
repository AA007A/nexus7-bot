"""Reproduce the Railway startup import path with PAPER_TRADE=true.

This closes a coverage gap: the existing sitecustomize test ran with the
CI default environment, while production validation runs explicitly in PAPER.
The subprocess preserves Python's automatic sitecustomize loading and emits
the exact startup marker/error if installation diverges under that env.
"""
import os
import subprocess
import sys
import unittest


class ProductionPaperStartupProbe(unittest.TestCase):
    def test_sitecustomize_is_ok_with_explicit_paper_mode(self):
        env = os.environ.copy()
        env["PAPER_TRADE"] = "true"
        env.pop("LIVE_TRADING_CONFIRMED", None)
        code = (
            "import builtins; "
            "print('STATUS=' + str(getattr(builtins, '_nexus_sitecustomize_status', 'missing')))"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            env=env,
            text=True,
            capture_output=True,
            timeout=30,
        )
        detail = f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
        self.assertEqual(proc.returncode, 0, detail)
        self.assertIn("STATUS=ok", proc.stdout, detail)


if __name__ == "__main__":
    unittest.main()
