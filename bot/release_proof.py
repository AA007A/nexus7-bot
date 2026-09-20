"""Single reproducible pre-release proof-pack entry point.

Each proof module runs in a fresh interpreter so runtime hardening installers and
class monkeypatches cannot leak state between otherwise independent proofs.
"""
import subprocess
import sys

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
    for module in MODULES:
        print(f"=== RELEASE PROOF: {module} ===", flush=True)
        completed = subprocess.run(
            [sys.executable, "-m", "unittest", module, "-v"],
            check=False,
        )
        if completed.returncode != 0:
            print(f"RELEASE_PROOF_FAILED={module}", file=sys.stderr)
            return completed.returncode or 1
    print("RELEASE_PROOF=PASS")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
