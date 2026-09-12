import unittest

from bot.service_readiness import evaluate_service_readiness


class ServiceReadinessSemanticsTests(unittest.TestCase):
    def test_healthy_service_is_ready(self):
        result = evaluate_service_readiness(
            bootstrap_complete=True,
            startup_blocked=False,
            durable_state_ok=True,
            instrument_count=12,
        )
        self.assertTrue(result.ready)
        self.assertEqual(result.reason, "service_healthy")

    def test_trading_active_is_not_an_infrastructure_input(self):
        # Deliberately no active/trading_ready argument exists. A drawdown or
        # integrity pause must not turn a healthy process into a dead container.
        result = evaluate_service_readiness(
            bootstrap_complete=True,
            startup_blocked=False,
            durable_state_ok=True,
            instrument_count=1,
        )
        self.assertTrue(result.ready)

    def test_startup_block_still_fails_closed(self):
        result = evaluate_service_readiness(
            bootstrap_complete=True,
            startup_blocked=True,
            durable_state_ok=True,
            instrument_count=12,
        )
        self.assertFalse(result.ready)
        self.assertEqual(result.reason, "startup_blocked")

    def test_durable_state_failure_is_not_ready(self):
        result = evaluate_service_readiness(
            bootstrap_complete=True,
            startup_blocked=False,
            durable_state_ok=False,
            instrument_count=12,
        )
        self.assertFalse(result.ready)
        self.assertEqual(result.reason, "durable_state_unhealthy")

    def test_missing_instruments_is_not_ready(self):
        result = evaluate_service_readiness(
            bootstrap_complete=True,
            startup_blocked=False,
            durable_state_ok=True,
            instrument_count=0,
        )
        self.assertFalse(result.ready)
        self.assertEqual(result.reason, "instruments_unavailable")

    def test_bootstrap_incomplete_is_not_ready(self):
        result = evaluate_service_readiness(
            bootstrap_complete=False,
            startup_blocked=False,
            durable_state_ok=True,
            instrument_count=12,
        )
        self.assertFalse(result.ready)
        self.assertEqual(result.reason, "bootstrap_incomplete")


if __name__ == "__main__":
    unittest.main()
