import os
import unittest
from unittest.mock import patch

from bot.execution_capability import (
    ExchangeMutationBlocked,
    assert_exchange_mutation_allowed,
)


class ShadowMutationBarrierTests(unittest.TestCase):
    def _blocked(self, method, endpoint):
        with patch.dict(os.environ, {"EXECUTION_CAPABILITY": "READ_ONLY"}, clear=False):
            with self.assertRaises(ExchangeMutationBlocked):
                assert_exchange_mutation_allowed(method, endpoint)

    def test_shadow_cannot_create_order(self):
        self._blocked("POST", "/api/v1/orders")

    def test_shadow_cannot_cancel_order(self):
        self._blocked("DELETE", "/api/v1/orders/123")

    def test_shadow_cannot_close_position(self):
        self._blocked("POST", "/api/v1/orders")

    def test_shadow_cannot_change_leverage(self):
        self._blocked("POST", "/api/v1/position/margin/auto-deposit-status")

    def test_shadow_valid_credentials_still_read_only(self):
        with patch.dict(os.environ, {
            "EXECUTION_CAPABILITY": "READ_ONLY",
            "KUCOIN_API_KEY": "valid-looking-key",
            "KUCOIN_API_SECRET": "valid-looking-secret",
            "KUCOIN_API_PASSPHRASE": "valid-looking-passphrase",
        }, clear=False):
            with self.assertRaises(ExchangeMutationBlocked):
                assert_exchange_mutation_allowed("POST", "/api/v1/orders")


if __name__ == "__main__":
    unittest.main()
