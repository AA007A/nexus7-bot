import unittest

from bot.service_readiness import EngineState, evaluate_deployment_readiness


def snap(**overrides):
    args = dict(
        bootstrap_complete=True,
        startup_blocked=False,
        durable_state_ok=True,
        instrument_count=12,
        worker_healthy=True,
        engine_state=EngineState.WAITING_FOR_EXECUTION_OWNERSHIP.value,
        db_authority_valid=True,
    )
    args.update(overrides)
    return evaluate_deployment_readiness(**args)


class DeploymentReadinessTests(unittest.TestCase):
    def test_existing_valid_owner_is_safe_standby(self):
        result = snap()
        self.assertTrue(result.deployment_ready)
        self.assertEqual(result.engine_state, "WAITING_FOR_EXECUTION_OWNERSHIP")

    def test_owned_candidate_is_deployment_ready(self):
        self.assertTrue(snap(engine_state="EXECUTION_OWNERSHIP_ACQUIRED").deployment_ready)

    def test_fatal_engine_is_not_deployment_ready(self):
        self.assertFalse(snap(engine_state="FAILED").deployment_ready)

    def test_db_authority_invalid_is_not_ready(self):
        self.assertFalse(snap(db_authority_valid=False).deployment_ready)

    def test_durable_restore_failure_is_not_ready(self):
        self.assertFalse(snap(durable_state_ok=False).deployment_ready)

    def test_instruments_failure_is_not_ready(self):
        self.assertFalse(snap(instrument_count=0).deployment_ready)

    def test_worker_failure_is_not_ready(self):
        self.assertFalse(snap(worker_healthy=False).deployment_ready)

    def test_starting_is_not_cutover_safe(self):
        self.assertFalse(snap(engine_state="STARTING").deployment_ready)


if __name__ == "__main__":
    unittest.main()
