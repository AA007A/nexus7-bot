import os
import unittest
from unittest.mock import patch

from bot.execution_capability import (
    ExchangeMutationBlocked,
    ExecutionCapability,
    assert_post_allowed,
    configured_capability,
)


class ExecutionCapabilityTests(unittest.TestCase):
    def test_shadow_valid_credentials_still_read_only(self):
        with patch.dict(os.environ, {
            "EXECUTION_CAPABILITY": "READ_ONLY",
            "KUCOIN_API_KEY": "valid-looking",
            "KUCOIN_API_SECRET": "valid-looking",
            "KUCOIN_API_PASSPHRASE": "valid-looking",
        }, clear=False):
            self.assertEqual(configured_capability(), ExecutionCapability.READ_ONLY)
            with self.assertRaises(ExchangeMutationBlocked):
                assert_post_allowed("/api/v1/orders")

    def test_shadow_cannot_create_order(self):
        with patch.dict(os.environ, {"EXECUTION_CAPABILITY": "READ_ONLY"}, clear=False):
            with self.assertRaises(ExchangeMutationBlocked):
                assert_post_allowed("/api/v1/orders")

    def test_shadow_cannot_cancel_order(self):
        with patch.dict(os.environ, {"EXECUTION_CAPABILITY": "READ_ONLY"}, clear=False):
            with self.assertRaises(ExchangeMutationBlocked):
                assert_post_allowed("/api/v1/orders/cancel")

    def test_shadow_cannot_close_position(self):
        with patch.dict(os.environ, {"EXECUTION_CAPABILITY": "READ_ONLY"}, clear=False):
            with self.assertRaises(ExchangeMutationBlocked):
                assert_post_allowed("/api/v1/orders")

    def test_shadow_cannot_change_leverage(self):
        with patch.dict(os.environ, {"EXECUTION_CAPABILITY": "READ_ONLY"}, clear=False):
            with self.assertRaises(ExchangeMutationBlocked):
                assert_post_allowed("/api/v1/position/margin/auto-deposit-status")

    def test_read_only_transport_allows_non_mutating_post(self):
        with patch.dict(os.environ, {"EXECUTION_CAPABILITY": "READ_ONLY"}, clear=False):
            assert_post_allowed("/api/v1/bullet-public")
            assert_post_allowed("/api/v2/batchGetCrossOrderLimit")

    def test_live_transport_allows_mutation(self):
        with patch.dict(os.environ, {"EXECUTION_CAPABILITY": "LIVE"}, clear=False):
            assert_post_allowed("/api/v1/orders")


if __name__ == "__main__":
    unittest.main()
