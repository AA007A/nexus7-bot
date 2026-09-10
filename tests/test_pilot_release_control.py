import os
import unittest
from unittest.mock import patch

from bot import pilot_release_control as release


class ControlledPilotReleaseTests(unittest.TestCase):
    def _full_env(self):
        return {
            "PAPER_TRADE": "false",
            "LIVE_TRADING_CONFIRMED": release.LIVE_TRADING_TOKEN,
            "REAL_TRADING_PILOT": "true",
            "PILOT_ACCOUNT_CONFIRMED": "true",
            "PILOT_RELEASE_APPROVED": release.PILOT_RELEASE_TOKEN,
            "VALIDATION_LOCK_RELEASE_APPROVED": release.VALIDATION_RELEASE_TOKEN,
        }

    def test_default_is_fail_closed(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(release.live_pilot_release_authorized())
            self.assertEqual(len(release.missing_release_checks()), 6)

    def test_exact_full_acknowledgement_authorizes(self):
        with patch.dict(os.environ, self._full_env(), clear=True):
            self.assertTrue(release.live_pilot_release_authorized())
            self.assertEqual(release.missing_release_checks(), [])

    def test_each_missing_acknowledgement_keeps_validation_lock_required(self):
        full = self._full_env()
        for missing in tuple(full):
            env = dict(full)
            env.pop(missing)
            with self.subTest(missing=missing), patch.dict(os.environ, env, clear=True):
                self.assertFalse(release.live_pilot_release_authorized())

    def test_wrong_release_tokens_fail_closed(self):
        full = self._full_env()
        for key in (
            "LIVE_TRADING_CONFIRMED",
            "PILOT_RELEASE_APPROVED",
            "VALIDATION_LOCK_RELEASE_APPROVED",
        ):
            env = dict(full)
            env[key] = "yes"
            with self.subTest(key=key), patch.dict(os.environ, env, clear=True):
                self.assertFalse(release.live_pilot_release_authorized())

    def test_paper_true_can_never_release_live(self):
        env = self._full_env()
        env["PAPER_TRADE"] = "true"
        with patch.dict(os.environ, env, clear=True):
            self.assertFalse(release.live_pilot_release_authorized())


if __name__ == "__main__":
    unittest.main()
