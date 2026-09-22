import math
import threading
import unittest
from types import SimpleNamespace
from bot.financial_state import FinancialStateInvalid, validate_financial_state
from bot.pilot import _validated_financial_state


class FinancialStateTests(unittest.TestCase):
    def test_valid_state(self):
        s = validate_financial_state(equity=20, available_margin=19, hwm=25, drawdown=.2)
        self.assertEqual(s.hwm, 25)

    def test_nan_rejected(self):
        with self.assertRaises(FinancialStateInvalid):
            validate_financial_state(equity=math.nan, available_margin=1, hwm=1, drawdown=0)

    def test_infinity_rejected(self):
        with self.assertRaises(FinancialStateInvalid):
            validate_financial_state(equity=1, available_margin=1, hwm=math.inf, drawdown=0)

    def test_negative_margin_rejected(self):
        with self.assertRaises(FinancialStateInvalid):
            validate_financial_state(equity=1, available_margin=-1, hwm=1, drawdown=0)

    def test_hwm_below_equity_rejected(self):
        with self.assertRaises(FinancialStateInvalid):
            validate_financial_state(equity=2, available_margin=1, hwm=1, drawdown=0)

    def test_drawdown_mismatch_rejected(self):
        with self.assertRaises(FinancialStateInvalid):
            validate_financial_state(equity=20, available_margin=19, hwm=25, drawdown=.1)

    def test_negative_equity_rejected(self):
        with self.assertRaises(FinancialStateInvalid):
            validate_financial_state(equity=-1, available_margin=0, hwm=1, drawdown=1)

    def test_negative_hwm_rejected(self):
        with self.assertRaises(FinancialStateInvalid):
            validate_financial_state(equity=0, available_margin=0, hwm=-1, drawdown=0)

    def test_negative_drawdown_rejected(self):
        with self.assertRaises(FinancialStateInvalid):
            validate_financial_state(equity=1, available_margin=1, hwm=1, drawdown=-.01)

    def test_near_zero_equity_is_well_defined_against_hwm(self):
        eq = 1e-12
        hwm = 25.0
        expected = (hwm - eq) / hwm
        s = validate_financial_state(equity=eq, available_margin=0, hwm=hwm, drawdown=expected)
        self.assertEqual(s.hwm, hwm)

    def test_explosive_hwm_detected_by_accounting_invariant(self):
        with self.assertRaises(FinancialStateInvalid):
            validate_financial_state(
                equity=19.1205130211, available_margin=19.1205130211,
                hwm=42_709_241_923.064377, drawdown=.70,
            )

    def test_pilot_financial_reader_uses_published_snapshot_not_legacy_available_shim(self):
        hwm = 63.7942573
        equity = 21.5075351411
        available = 11.9815151411
        drawdown = (hwm - equity) / hwm
        snapshot = validate_financial_state(
            equity=equity, available_margin=available, hwm=hwm, drawdown=drawdown
        )
        risk = SimpleNamespace(
            balance=available, peak_balance=hwm, drawdown=drawdown
        )
        engine = SimpleNamespace(
            risk=risk,
            _pilot_live_runtime_patched=True,
            _pilot_financial_state_snapshot=snapshot,
        )
        observed = _validated_financial_state(engine, risk.balance)
        self.assertEqual(observed, snapshot)
        self.assertAlmostEqual(observed.equity, equity)

    def test_live_pilot_missing_canonical_snapshot_fails_closed(self):
        engine = SimpleNamespace(
            risk=SimpleNamespace(balance=10.0, peak_balance=20.0, drawdown=0.5),
            _pilot_live_runtime_patched=True,
        )
        with self.assertRaisesRegex(
            FinancialStateInvalid, "pilot_financial_state_snapshot_unavailable"
        ):
            _validated_financial_state(engine, 10.0)

    def test_concurrent_snapshot_swaps_never_expose_torn_financial_state(self):
        state_a = validate_financial_state(
            equity=80.0, available_margin=30.0, hwm=100.0, drawdown=0.20
        )
        state_b = validate_financial_state(
            equity=60.0, available_margin=15.0, hwm=120.0, drawdown=0.50
        )
        engine = SimpleNamespace(
            risk=SimpleNamespace(balance=15.0),
            _pilot_live_runtime_patched=True,
            _pilot_financial_state_snapshot=state_a,
        )
        failures = []

        def writer():
            for i in range(5000):
                engine._pilot_financial_state_snapshot = state_a if i % 2 else state_b

        def reader():
            allowed = {
                (80.0, 30.0, 100.0, 0.20),
                (60.0, 15.0, 120.0, 0.50),
            }
            for _ in range(5000):
                try:
                    s = _validated_financial_state(engine, engine.risk.balance)
                    if (s.equity, s.available_margin, s.hwm, s.drawdown) not in allowed:
                        failures.append(s)
                except Exception as exc:
                    failures.append(exc)

        threads = [threading.Thread(target=writer)] + [
            threading.Thread(target=reader) for _ in range(3)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(failures, [])

if __name__ == "__main__":
    unittest.main()
