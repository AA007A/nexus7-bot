import os
import unittest
from unittest.mock import patch

from bot.execution_capability import (
    ExchangeMutationBlocked,
    assert_exchange_mutation_allowed,
)


class CriticalInvariantTests(unittest.TestCase):
    def test_shadow_can_never_mutate_exchange(self):
        with patch.dict(os.environ, {"EXECUTION_CAPABILITY": "READ_ONLY"}, clear=False):
            for method, endpoint in (
                ("POST", "/api/v1/orders"),
                ("DELETE", "/api/v1/orders/123"),
                ("POST", "/api/v1/position/margin/auto-deposit-status"),
                ("POST", "/api/v1/position/trading-stop"),
            ):
                with self.subTest(method=method, endpoint=endpoint):
                    with self.assertRaises(ExchangeMutationBlocked):
                        assert_exchange_mutation_allowed(method, endpoint)

    def test_live_capability_allows_transport_to_continue(self):
        with patch.dict(os.environ, {"EXECUTION_CAPABILITY": "LIVE"}, clear=False):
            self.assertIsNone(assert_exchange_mutation_allowed("POST", "/api/v1/orders"))


if __name__ == "__main__":
    unittest.main()
