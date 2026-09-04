"""Regression tests: REAL_TRADING_PILOT must never block or consume PAPER E2E."""
import os
import unittest
from unittest.mock import patch

from bot.pilot import PilotGuard


class PilotPaperIsolationTests(unittest.TestCase):
    @patch("bot.pilot.PILOT_ENABLED", True)
    @patch.dict(os.environ, {"PAPER_TRADE": "true"})
    def test_configured_real_pilot_is_inert_in_paper(self):
        guard = PilotGuard()
        self.assertFalse(guard.enabled)
        # Missing real-account/private-WS prerequisites are intentionally
        # irrelevant in PAPER and must not prevent the simulated E2E path.
        self.assertTrue(guard.can_open_pilot(object(), object(), "TESTUSDT", None))
        self.assertEqual(guard.state.blocked_reasons, [])

    @patch("bot.pilot.PILOT_ENABLED", True)
    @patch.dict(os.environ, {"PAPER_TRADE": "true"})
    def test_paper_does_not_consume_real_pilot_submission_budget(self):
        guard = PilotGuard()
        self.assertTrue(guard.reserve_submission("TESTUSDT"))
        guard.register_position_opened("TESTUSDT")
        self.assertEqual(guard.state.new_order_submissions_this_session, 0)
        self.assertEqual(guard.state.positions_opened_this_session, 0)

    @patch("bot.pilot.PILOT_ENABLED", True)
    @patch.dict(os.environ, {"PAPER_TRADE": "false"})
    def test_real_pilot_still_effective_outside_paper(self):
        guard = PilotGuard()
        self.assertTrue(guard.enabled)


if __name__ == "__main__":
    unittest.main()
