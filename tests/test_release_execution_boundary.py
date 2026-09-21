"""Pre-release proofs for execution ordering and stale-fence TOCTOU."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot import kucoin
from bot.execution_ownership import StaleExecutionFence
from tests.execution_test_context import ValidExecutionTestContext


class _Response:
    status = 200
    headers = {}
    async def json(self, content_type=None):
        return {"code": "200000", "data": {"orderId": "proof-order"}}


class _PostCM:
    def __init__(self, counter):
        self.counter = counter
    async def __aenter__(self):
        self.counter.append("exchange_post")
        return _Response()
    async def __aexit__(self, *args):
        return False


class _Session:
    def __init__(self, calls):
        self.calls = calls
    def post(self, *args, **kwargs):
        return _PostCM(self.calls)


class ReleaseExecutionBoundaryProofs(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = kucoin.KuCoinClient()
        self.client._instruments = {
            "BTCUSDT": {"tickSize": .1, "lotSize": 1, "qtyStep": 1,
                        "multiplier": .001, "minQty": 1, "minBaseQty": .001,
                        "minNotional": 0, "kucoinSymbol": "XBTUSDTM"}
        }

    async def test_execution_gate_order(self):
        calls = []
        ownership = object()
        self.client._engine = SimpleNamespace()
        async def acquire():
            calls.append("ownership")
            return ownership
        async def validate(_):
            return None
        def critical():
            calls.append("critical_state")
        def ready(_):
            calls.append("readiness")
        async def post(*args, **kwargs):
            calls.append("exchange_post")
            return {"orderId": "proof"}
        with patch.object(kucoin, "PAPER_TRADE", False), patch.object(kucoin, "API_KEY", "test"), \
             patch("bot.critical_state.critical_state.assert_available_for_new_risk", side_effect=critical), \
             patch("bot.execution_ownership.acquire_execution_ownership", side_effect=acquire), \
             patch("bot.execution_ownership.validate_execution_ownership", side_effect=validate), \
             patch("bot.runtime_readiness.assert_ready_for_new_entries", side_effect=ready), \
             patch.object(self.client, "_post", side_effect=post):
            await self.client.place_order("BTCUSDT", "Buy", .001)
        self.assertEqual(calls, ["critical_state", "ownership", "readiness", "exchange_post"])

    async def test_each_entry_gate_failure_prevents_exchange(self):
        scenarios = ("critical", "ownership", "readiness")
        for failure in scenarios:
            with self.subTest(failure=failure):
                self.client._execution_ownership = None
                self.client._engine = SimpleNamespace()
                post = AsyncMock()
                critical_side = RuntimeError("critical") if failure == "critical" else None
                acquire_side = RuntimeError("ownership") if failure == "ownership" else None
                ready_side = RuntimeError("readiness") if failure == "readiness" else None
                with patch.object(kucoin, "PAPER_TRADE", False), patch.object(kucoin, "API_KEY", "test"), \
                     patch("bot.critical_state.critical_state.assert_available_for_new_risk", side_effect=critical_side), \
                     patch("bot.execution_ownership.acquire_execution_ownership", new=AsyncMock(side_effect=acquire_side, return_value=object())), \
                     patch("bot.execution_ownership.validate_execution_ownership", new=AsyncMock()), \
                     patch("bot.runtime_readiness.assert_ready_for_new_entries", side_effect=ready_side), \
                     patch.object(self.client, "_post", post):
                    with self.assertRaises(RuntimeError):
                        await self.client.place_order("BTCUSDT", "Buy", .001)
                post.assert_not_awaited()

    async def test_takeover_after_pipeline_validation_is_rejected_at_http_boundary(self):
        calls = []
        self.client._session = _Session(calls)
        self.client._execution_ownership = object()
        self.client._ensure_session = AsyncMock()
        self.client._throttle = AsyncMock()
        self.client._auth_headers = lambda *a, **k: {}
        validations = 0
        async def validate(_):
            nonlocal validations
            validations += 1
            # Simulates takeover after the earlier pipeline validation: the
            # transport-boundary validation observes the now-stale token.
            raise StaleExecutionFence("REJECTED_STALE_FENCE superseded token")
        with patch.object(kucoin, "PAPER_TRADE", False), \
             patch("bot.execution_ownership.validate_execution_ownership", side_effect=validate):
            with self.assertRaises(StaleExecutionFence):
                await self.client._post("/api/v1/orders", {
                    "clientOid": "bgx7-proof", "symbol": "XBTUSDTM",
                    "side": "buy", "type": "market", "size": "1", "leverage": "50",
                }, single_attempt=True)
        self.assertEqual(validations, 1)
        self.assertEqual(calls, [], "stale owner must be rejected before session.post")


if __name__ == "__main__":
    unittest.main()
