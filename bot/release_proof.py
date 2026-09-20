"""Single reproducible pre-release proof-pack entry point.

The runner only orchestrates unittest modules; proof logic remains in tests.
"""
import sys
import unittest

MODULES = (
    "tests.test_release_execution_boundary",
    "tests.test_release_pilot_postgres",
    "tests.test_execution_ownership",
    "tests.test_critical_state",
    "tests.test_financial_state",
    "tests.test_runtime_readiness",
    "tests.test_live_adapter_chaos",
    "tests.test_durable_execution_restart",
    "tests.test_kucoin_native_tpsl",
    "tests.test_quantity_boundary",
)

def main() -> int:
    loader = unittest.defaultTestLoader
    suite = unittest.TestSuite(loader.loadTestsFromName(name) for name in MODULES)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if result.skipped:
        print("RELEASE_PROOF_BLOCKED: skipped mandatory proof tests", file=sys.stderr)
        return 2
    return 0 if result.wasSuccessful() else 1

if __name__ == "__main__":
    raise SystemExit(main())
