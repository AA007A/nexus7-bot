"""Single reproducible pre-release proof-pack entry point."""
import subprocess
import sys

MODULES = (
    "tests.test_release_execution_boundary",
    "tests.test_final_release_proof",
    "tests.test_final_evidence_closure",
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
    "tests.test_regression",
)

def main() -> int:
    completed = subprocess.run(
        [sys.executable, "-m", "tests.run_offline", *MODULES],
        check=False,
    )
    if completed.returncode != 0:
        print("RELEASE_PROOF=FAIL", file=sys.stderr)
        return completed.returncode or 1
    print("RELEASE_PROOF=PASS")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
