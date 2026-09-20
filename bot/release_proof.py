"""Single reproducible pre-release proof-pack entry point."""
import subprocess
import sys

UNITTEST_MODULES = (
    "tests.test_release_execution_boundary",
    "tests.test_final_release_proof",
    "tests.test_release_pilot_postgres",
    "tests.test_execution_ownership",
    "tests.test_critical_state",
    "tests.test_financial_state",
    "tests.test_runtime_readiness",
    "tests.test_live_adapter_chaos",
    "tests.test_durable_execution_restart",
    "tests.test_durable_reconcile_hardening",
    "tests.test_restart_opening_order_lineage",
    "tests.test_kucoin_native_tpsl",
    "tests.test_native_stop_repair",
    "tests.test_release_source_regression",
    "tests.test_quantity_boundary",
)

def main() -> int:
    for module in UNITTEST_MODULES:
        print(f"=== RELEASE PROOF: {module} ===", flush=True)
        completed = subprocess.run([sys.executable, "-m", "unittest", module, "-v"], check=False)
        if completed.returncode != 0:
            print(f"RELEASE_PROOF_FAILED={module}", file=sys.stderr)
            return completed.returncode or 1
    completed = subprocess.run(
        [sys.executable, "-m", "tests.run_offline", "tests.test_regression"],
        check=False,
    )
    if completed.returncode != 0:
        print("RELEASE_PROOF_FAILED=tests.test_regression", file=sys.stderr)
        return completed.returncode or 1
    print("RELEASE_PROOF=PASS")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
