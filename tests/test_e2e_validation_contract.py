import unittest

from bot.e2e_validation_contract import required_evidence, validate_e2e_evidence


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


if __name__ == "__main__":
    unittest.main()
