import unittest
from types import SimpleNamespace

from bot.status_observability import execution_observability


class _Pilot:
    def __init__(self, *, enabled=True, release_approved=False, fail=False):
        self.enabled = enabled
        self.release_approved = release_approved
        self.fail = fail

    def status(self, engine=None, client=None):
        if self.fail:
            raise RuntimeError("status unavailable")
        return {"release_approved": self.release_approved}


class StatusExecutionObservabilityTests(unittest.TestCase):
    def _engine(self, **kwargs):
        defaults = {
            "paper_trade": False,
            "_validation_safety_lock_active": False,
            "pilot": None,
            "client": object(),
        }
        defaults.update(kwargs)
        return SimpleNamespace(**defaults)

    def test_paper_never_reports_exchange_orders(self):
        state = execution_observability(self._engine(paper_trade=True))
        self.assertEqual(state["effective_execution_mode"], "PAPER")
        self.assertFalse(state["orders_sent_to_exchange"])

    def test_validation_lock_reports_shadow_live(self):
        state = execution_observability(
            self._engine(_validation_safety_lock_active=True)
        )
        self.assertEqual(state["effective_execution_mode"], "SHADOW_LIVE")
        self.assertFalse(state["orders_sent_to_exchange"])
        self.assertEqual(state["execution_effect"], "NONE")

    def test_pilot_without_release_is_locked(self):
        state = execution_observability(
            self._engine(pilot=_Pilot(release_approved=False))
        )
        self.assertEqual(state["effective_execution_mode"], "LIVE_LOCKED")
        self.assertFalse(state["orders_sent_to_exchange"])

    def test_pilot_status_failure_is_fail_closed(self):
        state = execution_observability(self._engine(pilot=_Pilot(fail=True)))
        self.assertEqual(state["effective_execution_mode"], "LIVE_LOCKED")
        self.assertFalse(state["orders_sent_to_exchange"])

    def test_released_live_reports_real_execution(self):
        state = execution_observability(
            self._engine(pilot=_Pilot(release_approved=True))
        )
        self.assertEqual(state["effective_execution_mode"], "LIVE")
        self.assertTrue(state["orders_sent_to_exchange"])
        self.assertEqual(state["execution_effect"], "REAL")


if __name__ == "__main__":
    unittest.main()
