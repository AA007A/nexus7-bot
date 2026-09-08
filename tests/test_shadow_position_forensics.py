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


class _Client:
    async def get_positions(self):
        return [{
            "symbol": "ADAUSDT", "side": "Buy", "size": 10,
            "entryPrice": 0.40, "markPrice": 0.35,
            "liquidationPrice": 0.30, "leverage": 10,
            "unrealisedPnl": -0.5, "posMargin": 2.0,
            "stopLoss": 0.0, "takeProfit": 0.0,
        }]


class ShadowPositionForensicsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        if hasattr(_Client, "_shadow_position_forensics_installed"):
            delattr(_Client, "_shadow_position_forensics_installed")
        self.log = _Log()
        shadow_position_forensics.install(SimpleNamespace(KuCoinClient=_Client), self.log)

    async def test_shadow_logs_normalized_forensics(self):
        builtins._validation_safety_lock_active = True
        c = _Client()
        rows = await c.get_positions()
        self.assertEqual(rows[0]["symbol"], "ADAUSDT")
        warnings = [m for level, m in self.log.messages if level == "warning"]
        self.assertTrue(any("[SHADOW_POSITION_FORENSICS]" in str(m) for m in warnings))

    async def test_outside_shadow_is_passthrough(self):
        builtins._validation_safety_lock_active = False
        c = _Client()
        rows = await c.get_positions()
        self.assertEqual(len(rows), 1)
        warnings = [m for level, m in self.log.messages if level == "warning"]
        self.assertFalse(any("symbol=%s" in str(m) for m in warnings))


if __name__ == "__main__":
    unittest.main()
