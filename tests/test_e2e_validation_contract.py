import io
import json
import unittest
from contextlib import redirect_stdout

from bot.e2e_validation_contract import main, required_evidence, validate_e2e_evidence


class E2EValidationContractTests(unittest.TestCase):
    def test_complete_ordered_evidence_passes(self):
        result = validate_e2e_evidence(required_evidence())
        self.assertTrue(result.passed)

    def test_missing_protection_fails(self):
        events = [e for e in required_evidence() if e != "PROTECTION_CONFIRMED"]
        result = validate_e2e_evidence(events)
        self.assertFalse(result.passed)
        self.assertIn("PROTECTION_CONFIRMED", result.missing)

    def test_out_of_order_evidence_fails(self):
        events = list(required_evidence())
        events[3], events[4] = events[4], events[3]
        result = validate_e2e_evidence(events)
        self.assertFalse(result.passed)
        self.assertTrue(result.out_of_order)

    def test_cli_required_is_read_only_and_explicit(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["--required"])
        payload = json.loads(out.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(tuple(payload["required"]), required_evidence())
        self.assertEqual(payload["decision_effect"], "NONE")
        self.assertEqual(payload["execution_effect"], "NONE")

    def test_cli_validation_exit_codes(self):
        passed_out = io.StringIO()
        with redirect_stdout(passed_out):
            passed_code = main(list(required_evidence()))
        self.assertEqual(passed_code, 0)
        self.assertTrue(json.loads(passed_out.getvalue())["passed"])

        failed_out = io.StringIO()
        with redirect_stdout(failed_out):
            failed_code = main([])
        failed_payload = json.loads(failed_out.getvalue())
        self.assertEqual(failed_code, 1)
        self.assertFalse(failed_payload["passed"])
        self.assertEqual(failed_payload["execution_effect"], "NONE")


if __name__ == "__main__":
    unittest.main()
