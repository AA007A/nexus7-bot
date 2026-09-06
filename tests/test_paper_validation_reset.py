import json
import os
import unittest
from unittest.mock import patch

from bot import paper_validation_reset as reset


class PaperValidationResetTests(unittest.TestCase):
    def test_fresh_payload_clears_only_paper_runtime_state(self):
        payload = json.loads(reset._fresh_runtime_payload(20.0, "validation-1"))
        self.assertEqual(payload["version"], 1)
        self.assertEqual(payload["balance"], 20.0)
        self.assertEqual(payload["peak_balance"], 20.0)
        self.assertEqual(payload["positions"], [])
        self.assertEqual(payload["trade_ids"], {})
        self.assertEqual(payload["cooldown"], {})
        self.assertIn("validation-1", payload["reason"])

    def test_wallet_payload_resets_drawdown_baseline(self):
        payload = json.loads(reset._fresh_wallet_payload(20.0, "validation-2"))
        self.assertEqual(payload["balance"], payload["peak_balance"])
        self.assertEqual(payload["balance"], 20.0)

    def test_reset_request_is_explicit_and_bounded(self):
        with patch.dict(os.environ, {"PAPER_RESET_STATE_ONCE": "x" * 500}, clear=False):
            request_id = reset._requested_reset_id()
        self.assertEqual(len(request_id), 160)


if __name__ == "__main__":
    unittest.main()
