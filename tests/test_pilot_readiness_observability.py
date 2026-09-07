import os
import unittest
from unittest.mock import patch

from bot import pilot
from bot import pilot_readiness_observability as obs


class PilotReadinessObservabilityTests(unittest.TestCase):
    def test_live_pilot_without_release_reports_blocked_boolean_only(self):
        with patch.object(pilot, "PILOT_ENABLED", True), patch.dict(
            os.environ,
            {"PAPER_TRADE": "false", "PILOT_RELEASE_APPROVED": ""},
            clear=False,
        ):
            state = obs.snapshot()
        self.assertEqual(
            state,
            {
                "configured": True,
                "enabled": True,
                "paper_trade": False,
                "release_approved": False,
            },
        )

    def test_paper_never_reports_pilot_enabled(self):
        with patch.object(pilot, "PILOT_ENABLED", True), patch.dict(
            os.environ,
            {
                "PAPER_TRADE": "true",
                "PILOT_RELEASE_APPROVED": pilot.PILOT_RELEASE_TOKEN,
            },
            clear=False,
        ):
            state = obs.snapshot()
        self.assertFalse(state["enabled"])
        self.assertTrue(state["paper_trade"])

    def test_snapshot_contains_no_secret_or_token_value(self):
        with patch.object(pilot, "PILOT_ENABLED", True), patch.dict(
            os.environ,
            {
                "PAPER_TRADE": "false",
                "PILOT_RELEASE_APPROVED": pilot.PILOT_RELEASE_TOKEN,
            },
            clear=False,
        ):
            rendered = repr(obs.snapshot())
        self.assertNotIn(pilot.PILOT_RELEASE_TOKEN, rendered)
        self.assertNotIn("KUCOIN_API", rendered)


if __name__ == "__main__":
    unittest.main()
