import builtins
import unittest
from types import SimpleNamespace

from bot import shadow_position_forensics


class _Log:
    def __init__(self):
        self.messages = []
    def info(self, *args):
        self.messages.append(("info", args))
    def warning(self, *args):
        self.messages.append(("warning", args))
    def error(self, *args):
        self.messages.append(("error", args))


def _client_class():
    class Client:
        async def get_positions(self):
            return [{
                "symbol": "ADAUSDT", "side": "Buy", "size": 10,
                "entryPrice": 0.40, "markPrice": 0.35,
                "liquidationPrice": 0.30, "leverage": 10,
                "unrealisedPnl": -0.5, "posMargin": 2.0,
                "stopLoss": 0.0, "takeProfit": 0.0,
            }]
    return Client


class ShadowPositionForensicsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.log = _Log()
        self.Client = _client_class()
        shadow_position_forensics.install(
            SimpleNamespace(KuCoinClient=self.Client), self.log
        )

    async def asyncTearDown(self):
        builtins._validation_safety_lock_active = False

    async def test_shadow_logs_normalized_forensics(self):
        builtins._validation_safety_lock_active = True
        c = self.Client()
        rows = await c.get_positions()
        self.assertEqual(rows[0]["symbol"], "ADAUSDT")
        warnings = [m for level, m in self.log.messages if level == "warning"]
        self.assertTrue(any("[SHADOW_POSITION_FORENSICS]" in str(m) for m in warnings))

    async def test_outside_shadow_is_passthrough(self):
        builtins._validation_safety_lock_active = False
        c = self.Client()
        rows = await c.get_positions()
        self.assertEqual(len(rows), 1)
        warnings = [m for level, m in self.log.messages if level == "warning"]
        self.assertFalse(any("symbol=%s" in str(m) for m in warnings))


if __name__ == "__main__":
    unittest.main()
