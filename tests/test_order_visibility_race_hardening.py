import asyncio
import unittest
from types import SimpleNamespace

from bot import order_visibility_race_hardening as hardening


class _Registry:
    def __init__(self, order):
        self.order = order

    def get_by_order_id(self, order_id):
        return self.order


class OrderVisibilityRaceHardeningTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.old_grace = hardening.WS_AHEAD_GRACE_S
        self.old_poll = hardening.POLL_S
        self.old_settle = hardening.FILLED_SETTLE_S
        self.old_min = hardening.MIN_REST_TIMEOUT_S
        hardening.WS_AHEAD_GRACE_S = 0.02
        hardening.POLL_S = 0.002
        hardening.FILLED_SETTLE_S = 0.002
        hardening.MIN_REST_TIMEOUT_S = 0.001

    def tearDown(self):
        hardening.WS_AHEAD_GRACE_S = self.old_grace
        hardening.POLL_S = self.old_poll
        hardening.FILLED_SETTLE_S = self.old_settle
        hardening.MIN_REST_TIMEOUT_S = self.old_min

    async def test_unknown_ws_state_calls_rest_without_grace_and_preserves_result(self):
        calls = []

        class Client:
            async def wait_for_fill(self, order_id, timeout_s=8.0, poll_interval_s=0.5):
                calls.append((order_id, timeout_s, poll_interval_s))
                return {"filled": True, "source": "REST"}

        original = Client.wait_for_fill
        hardening.install(Client, SimpleNamespace(info=lambda *a, **k: None))
        c = Client()
        c._order_registry = _Registry(SimpleNamespace(state=SimpleNamespace(value="SUBMITTED")))
        result = await c.wait_for_fill("123", timeout_s=0.05, poll_interval_s=0.01)
        self.assertEqual({"filled": True, "source": "REST"}, result)
        self.assertEqual("123", calls[0][0])
        self.assertLessEqual(calls[0][1], 0.05)
        self.assertIsNot(Client.wait_for_fill, original)

    async def test_partial_then_filled_delays_rest_but_never_synthesizes_fill(self):
        calls = []
        order = SimpleNamespace(state=SimpleNamespace(value="PARTIALLY_FILLED"))

        class Client:
            async def wait_for_fill(self, order_id, timeout_s=8.0, poll_interval_s=0.5):
                calls.append(timeout_s)
                return {"filled": False, "status": {"_unknown": True}, "timed_out": True}

        hardening.install(Client, SimpleNamespace(info=lambda *a, **k: None))
        c = Client()
        c._order_registry = _Registry(order)

        async def promote():
            await asyncio.sleep(0.004)
            order.state = SimpleNamespace(value="FILLED")

        task = asyncio.create_task(promote())
        result = await c.wait_for_fill("456", timeout_s=0.05, poll_interval_s=0.01)
        await task
        self.assertFalse(result["filled"])
        self.assertTrue(result["timed_out"])
        self.assertGreater(calls[0], 0)
        self.assertLess(calls[0], 0.05)

    async def test_timeout_budget_is_deducted_from_grace(self):
        calls = []

        class Client:
            async def wait_for_fill(self, order_id, timeout_s=8.0, poll_interval_s=0.5):
                calls.append(timeout_s)
                return {"filled": True}

        hardening.install(Client, SimpleNamespace(info=lambda *a, **k: None))
        c = Client()
        c._order_registry = _Registry(SimpleNamespace(state=SimpleNamespace(value="PARTIALLY_FILLED")))
        await c.wait_for_fill("789", timeout_s=0.03, poll_interval_s=0.01)
        self.assertGreaterEqual(calls[0], hardening.MIN_REST_TIMEOUT_S)
        self.assertLess(calls[0], 0.03)

    def test_install_is_idempotent(self):
        class Client:
            async def wait_for_fill(self, order_id, timeout_s=8.0, poll_interval_s=0.5):
                return {"filled": True}

        logger = SimpleNamespace(info=lambda *a, **k: None)
        hardening.install(Client, logger)
        once = Client.wait_for_fill
        hardening.install(Client, logger)
        self.assertIs(once, Client.wait_for_fill)


if __name__ == "__main__":
    unittest.main()
