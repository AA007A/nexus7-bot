import os
import unittest
from unittest.mock import patch

from bot import pilot
from bot import validation_safety_lock


class ValidationLockReleaseBoundaryTests(unittest.TestCase):
    def _env(self, **overrides):
        env = {
            "PAPER_TRADE": "false",
            "REAL_TRADING_PILOT": "true",
            "PILOT_ACCOUNT_CONFIRMED": "true",
            "LIVE_TRADING_CONFIRMED": "I_UNDERSTAND_THE_RISK",
            "PILOT_RELEASE_APPROVED": pilot.PILOT_RELEASE_TOKEN,
        }
        env.update(overrides)
        return patch.dict(os.environ, env, clear=False)

    def test_exact_complete_release_state_bypasses_shadow_lock(self):
        with self._env():
            self.assertTrue(validation_safety_lock._live_pilot_release_approved())

    def test_missing_release_token_keeps_fail_closed_lock(self):
        with self._env(PILOT_RELEASE_APPROVED=""):
            self.assertFalse(validation_safety_lock._live_pilot_release_approved())

    def test_wrong_release_token_keeps_fail_closed_lock(self):
        with self._env(PILOT_RELEASE_APPROVED="I_APPROVE_ONE_LIVE_PILOT_ORDER"):
            self.assertFalse(validation_safety_lock._live_pilot_release_approved())

    def test_paper_mode_never_uses_live_release_boundary(self):
        with self._env(PAPER_TRADE="true"):
            self.assertFalse(validation_safety_lock._live_pilot_release_approved())

    def test_missing_account_confirmation_keeps_lock(self):
        with self._env(PILOT_ACCOUNT_CONFIRMED="false"):
            self.assertFalse(validation_safety_lock._live_pilot_release_approved())

    def test_missing_live_confirmation_keeps_lock(self):
        with self._env(LIVE_TRADING_CONFIRMED=""):
            self.assertFalse(validation_safety_lock._live_pilot_release_approved())

    def test_pilot_disabled_keeps_lock(self):
        with self._env(REAL_TRADING_PILOT="false"):
            self.assertFalse(validation_safety_lock._live_pilot_release_approved())


if __name__ == "__main__":
    unittest.main()
