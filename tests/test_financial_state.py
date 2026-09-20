import math
import unittest
from bot.financial_state import FinancialStateInvalid, validate_financial_state


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


if __name__ == "__main__":
    unittest.main()
