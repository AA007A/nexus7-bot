import unittest
from types import SimpleNamespace
from unittest.mock import patch

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
            "connected": True,
            "active": True,
            "risk": SimpleNamespace(drawdown=0.0),
        }
        defaults.update(kwargs)
        return SimpleNamespace(**defaults)

    def test_paper_never_reports_exchange_orders(self):
        state = execution_observability(self._engine(paper_trade=True))
        self.assertEqual(state["effective_execution_mode"], "PAPER")
        self.assertFalse(state["orders_sent_to_exchange"])
        self.assertFalse(state["new_entries_allowed"])

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
        self.assertTrue(state["new_entries_allowed"])
        self.assertEqual(state["execution_effect"], "REAL")

    def test_drawdown_hard_gate_reports_live_blocked(self):
        from bot.config import cfg

        engine = self._engine(
            pilot=_Pilot(release_approved=True),
            risk=SimpleNamespace(drawdown=0.7218),
        )
        with patch.object(cfg, "MAX_DRAWDOWN", 0.10), patch.dict(
            "os.environ", {"LIVE_RISK_OVERRIDE_APPROVED": "false"}, clear=False
        ):
            state = execution_observability(engine)

        self.assertEqual(state["effective_execution_mode"], "LIVE_BLOCKED")
        self.assertFalse(state["orders_sent_to_exchange"])
        self.assertFalse(state["new_entries_allowed"])
        self.assertIn("DRAWDOWN_HARD_GATE", state["execution_blockers"])
        self.assertEqual(state["drawdown_pct"], 72.18)
        self.assertEqual(state["drawdown_limit_pct"], 10.0)

    def test_disconnected_live_reports_blocked(self):
        state = execution_observability(
            self._engine(pilot=_Pilot(release_approved=True), connected=False)
        )
        self.assertEqual(state["effective_execution_mode"], "LIVE_BLOCKED")
        self.assertIn("ENGINE_DISCONNECTED", state["execution_blockers"])
        self.assertFalse(state["new_entries_allowed"])


if __name__ == "__main__":
    unittest.main()
