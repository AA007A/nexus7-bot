import unittest
from pathlib import Path
from types import SimpleNamespace

from bot import shadow_position_forensics


ROOT = Path(__file__).resolve().parents[1]


class _Log:
    def __init__(self):
        self.messages = []

    def info(self, *args):
        self.messages.append(("info", args))

    def warning(self, *args):
        self.messages.append(("warning", args))

    def error(self, *args):
        self.messages.append(("error", args))


def _row(symbol="ADAUSDT", side="Buy", size=10, mark=0.35, stop=0.0):
    return {
        "symbol": symbol,
        "side": side,
        "size": size,
        "entryPrice": 0.40,
        "markPrice": mark,
        "liquidationPrice": 0.30,
        "leverage": 10,
        "unrealisedPnl": -0.5,
        "posMargin": 2.0,
        "stopLoss": stop,
        "takeProfit": 0.0,
    }


def _client_class(initial_rows=None):
    class Client:
        def __init__(self):
            self.rows = list(initial_rows or [_row()])

        async def get_positions(self):
            return [dict(row) for row in self.rows]

    return Client


class ShadowPositionForensicsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.log = _Log()
        self.Client = _client_class()
        shadow_position_forensics.install(
            SimpleNamespace(KuCoinClient=self.Client), self.log
        )

    def _forensic_warnings(self):
        return [
            args for level, args in self.log.messages
            if level == "warning" and "[SHADOW_POSITION_FORENSICS]" in str(args)
        ]

    async def test_shadow_logs_normalized_forensics_from_client_flag(self):
        c = self.Client()
        c._shadow_readonly_active = True
        rows = await c.get_positions()
        self.assertEqual(rows[0]["symbol"], "ADAUSDT")
        self.assertEqual(len(self._forensic_warnings()), 1)
        self.assertIn("read_only=true execution_effect=NONE", self._forensic_warnings()[0][0])

    async def test_outside_shadow_is_passthrough(self):
        c = self.Client()
        rows = await c.get_positions()
        self.assertEqual(len(rows), 1)
        self.assertEqual(self._forensic_warnings(), [])
        self.assertFalse(hasattr(c, "_shadow_forensics_last_by_symbol"))

    async def test_two_positions_are_each_logged_once_when_unchanged(self):
        Client = _client_class([
            _row("ATOMUSDT", size=3, mark=1.80),
            _row("BTCUSDT", size=2, mark=112000.0),
        ])
        log = _Log()
        shadow_position_forensics.install(SimpleNamespace(KuCoinClient=Client), log)
        c = Client()
        c._shadow_readonly_active = True

        first = await c.get_positions()
        second = await c.get_positions()

        self.assertEqual(first, second)
        warnings = [
            args for level, args in log.messages
            if level == "warning" and "[SHADOW_POSITION_FORENSICS]" in str(args)
        ]
        self.assertEqual(len(warnings), 2)
        rendered = " ".join(str(args) for args in warnings)
        self.assertIn("ATOMUSDT", rendered)
        self.assertIn("BTCUSDT", rendered)

    async def test_dynamic_market_fields_do_not_spam_structural_snapshot(self):
        c = self.Client()
        c._shadow_readonly_active = True
        await c.get_positions()
        c.rows[0]["markPrice"] = 0.36
        c.rows[0]["unrealisedPnl"] = -0.4
        c.rows[0]["leverage"] = 10.25
        c.rows[0]["posMargin"] = 2.15
        await c.get_positions()
        self.assertEqual(len(self._forensic_warnings()), 1)

    async def test_protection_change_emits_fresh_snapshot(self):
        c = self.Client()
        c._shadow_readonly_active = True
        await c.get_positions()
        c.rows[0]["stopLoss"] = 0.33
        await c.get_positions()
        self.assertEqual(len(self._forensic_warnings()), 2)

    async def test_closed_then_reopened_symbol_logs_again(self):
        c = self.Client()
        c._shadow_readonly_active = True
        await c.get_positions()
        c.rows = []
        await c.get_positions()
        c.rows = [_row()]
        await c.get_positions()
        self.assertEqual(len(self._forensic_warnings()), 2)

    def test_validation_lock_sets_client_shadow_flag_only_after_nonpaper_branch(self):
        text = (ROOT / "bot" / "validation_safety_lock.py").read_text(encoding="utf-8")
        paper_guard = 'if getattr(self, "paper_trade", False):\n            return await original_connect'
        client_flag = "self.client._shadow_readonly_active = True"
        self.assertIn(paper_guard, text)
        self.assertIn(client_flag, text)
        self.assertGreater(text.index(client_flag), text.index(paper_guard))

    def test_forensics_has_no_exchange_mutation_or_global_validation_flag(self):
        text = (ROOT / "bot" / "shadow_position_forensics.py").read_text(encoding="utf-8")
        self.assertNotIn("import builtins", text)
        self.assertNotIn("_validation_safety_lock_active", text)
        for marker in (
            "place_order(",
            "cancel_order(",
            "cancel_all_orders(",
            "set_position_stops(",
            "set_leverage(",
            "close_position(",
            "execution_effect=SUBMIT",
            "execution_effect=MUTATE",
        ):
            self.assertNotIn(marker, text)


if __name__ == "__main__":
    unittest.main()
