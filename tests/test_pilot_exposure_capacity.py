import time
import unittest
from types import SimpleNamespace

from bot import pilot_exposure_capacity as cap


class _State:
    def __init__(self, issues):
        self.issues = issues


class _Integrity:
    def __init__(self, issues):
        self.state = _State(issues)


class _PilotState:
    blocked_reasons = []


class _PilotGuard:
    _exposure_capacity_patched = False

    def __init__(self):
        self.state = _PilotState()
        self.enabled = True

    def evaluate(self, engine, client, symbol, ai_decision=None):
        return []


class PilotExposureCapacityTests(unittest.TestCase):
    def setUp(self):
        _PilotGuard._exposure_capacity_patched = False
        _PilotGuard.evaluate = lambda self, engine, client, symbol, ai_decision=None: []
        cap.install(_PilotGuard, SimpleNamespace(warning=lambda *a, **k: None))

    def _engine(self, issues=None, positions=None):
        return SimpleNamespace(
            positions=positions or {},
            integrity=_Integrity(issues or []),
        )

    def _client(
        self,
        available=50.0,
        equity=100.0,
        position_margin=20.0,
        order_margin=0.0,
        available_margin=None,
        age=0.0,
    ):
        snap = {
            "accountEquity": equity,
            "availableBalance": available,
            "positionMargin": position_margin,
            "orderMargin": order_margin,
            "_observed_at": time.time() - age,
        }
        if available_margin is not None:
            snap["availableMargin"] = available_margin
        return SimpleNamespace(_last_account_overview_snapshot=snap)

    def test_external_position_counts_toward_pilot_concurrency(self):
        issue = SimpleNamespace(
            code="EXTERNAL_POSITION_PROTECTED",
            detail="XRPUSDT: posição externa protegida",
        )
        guard = _PilotGuard()
        reasons = guard.evaluate(
            self._engine([issue], positions={"BTCUSDT": object()}),
            self._client(),
            "ETHUSDT",
        )
        self.assertTrue(any(r.startswith("PILOT_TOTAL_CONCURRENT:") for r in reasons))

    def test_two_external_positions_fill_pilot_capacity(self):
        issues = [
            SimpleNamespace(
                code="EXTERNAL_POSITION_PROTECTED",
                detail="XRPUSDT: posição externa protegida",
            ),
            SimpleNamespace(
                code="EXTERNAL_POSITION_PROTECTED",
                detail="BTCUSDT: posição externa protegida",
            ),
        ]
        guard = _PilotGuard()
        reasons = guard.evaluate(self._engine(issues), self._client(), "ETHUSDT")
        self.assertTrue(any(r.startswith("PILOT_TOTAL_CONCURRENT:") for r in reasons))

    def test_one_external_position_alone_leaves_one_pilot_slot(self):
        issue = SimpleNamespace(
            code="EXTERNAL_POSITION_PROTECTED",
            detail="XRPUSDT: posição externa protegida",
        )
        guard = _PilotGuard()
        reasons = guard.evaluate(self._engine([issue]), self._client(), "ETHUSDT")
        self.assertFalse(any(r.startswith("PILOT_TOTAL_CONCURRENT:") for r in reasons))

    def test_low_available_equity_blocks(self):
        guard = _PilotGuard()
        reasons = guard.evaluate(self._engine(), self._client(available=10.0), "ETHUSDT")
        self.assertTrue(any(r.startswith("PILOT_AVAILABLE_CAPACITY:") for r in reasons))

    def test_cross_margin_available_margin_is_preferred(self):
        guard = _PilotGuard()
        reasons = guard.evaluate(
            self._engine(),
            self._client(available=90.0, available_margin=10.0),
            "ETHUSDT",
        )
        self.assertTrue(any(r.startswith("PILOT_AVAILABLE_CAPACITY:") for r in reasons))

    def test_high_position_margin_blocks(self):
        guard = _PilotGuard()
        reasons = guard.evaluate(
            self._engine(), self._client(available=10.0, position_margin=90.0), "ETHUSDT"
        )
        self.assertTrue(any(r.startswith("PILOT_MARGIN_CAPACITY:") for r in reasons))

    def test_negative_legacy_position_margin_cannot_bypass_capacity_gate(self):
        guard = _PilotGuard()
        reasons = guard.evaluate(
            self._engine(),
            self._client(
                equity=100.0,
                available=90.0,
                available_margin=10.0,
                position_margin=-25.0,
                order_margin=0.0,
            ),
            "ETHUSDT",
        )
        self.assertTrue(any(r.startswith("PILOT_MARGIN_CAPACITY:") for r in reasons))

    def test_nonfinite_capital_is_fail_closed(self):
        guard = _PilotGuard()
        reasons = guard.evaluate(
            self._engine(), self._client(equity=float("nan")), "ETHUSDT"
        )
        self.assertTrue(any(r.startswith("PILOT_CAPITAL_VALUES:") for r in reasons))

    def test_missing_snapshot_is_fail_closed(self):
        guard = _PilotGuard()
        reasons = guard.evaluate(self._engine(), SimpleNamespace(), "ETHUSDT")
        self.assertIn(
            "PILOT_CAPITAL_SNAPSHOT: account overview ausente (fail-closed)", reasons
        )

    def test_safe_empty_account_adds_no_capacity_reason(self):
        guard = _PilotGuard()
        reasons = guard.evaluate(self._engine(), self._client(), "ETHUSDT")
        self.assertEqual(reasons, [])


if __name__ == "__main__":
    unittest.main()
