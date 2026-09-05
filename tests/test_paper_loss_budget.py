import ast
from pathlib import Path
import unittest

from bot.paper_loss_budget import cap_quantity

ROOT = Path(__file__).resolve().parents[1]


class PaperLossBudgetTests(unittest.TestCase):
    def test_engine_budget_calls_are_paper_only(self):
        tree = ast.parse((ROOT / "bot/engine.py").read_text())
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Name) and n.func.id == "cap_paper_quantity"]
        self.assertEqual(len(calls), 2)
        guards = [n for n in ast.walk(tree) if isinstance(n, ast.If)
                  and ast.unparse(n.test) == "self.paper_trade"]
        for call in calls:
            self.assertTrue(any(call in list(ast.walk(g)) for g in guards))

    def cap(self, qty=18.7, balance=20, entry=51.85, stop=51.621011,
            direction="LONG", info=None):
        return cap_quantity(qty, balance, entry, stop, direction,
                            info or {"multiplier": "0.1", "lotSize": 1, "minQty": 1},
                            0.0006)

    def test_ltc_is_still_allowed_at_seventy_percent(self):
        self.assertEqual(self.cap(), 18.7)

    def test_large_order_reduced(self):
        qty = self.cap(qty=1000)
        self.assertLess(qty, 1000)
        unit = abs(51.85-51.621011) + (51.85+51.621011)*(.0005+1.0005*.0006)
        self.assertLessEqual(qty*unit, 14)
        self.assertGreater((qty+.1)*unit, 14)

    def test_short(self):
        self.assertLess(self.cap(qty=1000, stop=52.1, direction="SHORT"), 1000)

    def test_minimum_never_forced(self):
        with self.assertRaises(ValueError):
            self.cap(balance=.001)

    def test_invalid_inputs_block(self):
        for value in (0, -1, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.cap(balance=value)
        with self.assertRaises(ValueError):
            self.cap(stop=52)

    def test_min_notional_blocks(self):
        with self.assertRaises(ValueError):
            self.cap(info={"multiplier": ".1", "minQty": 1,
                           "lotSize": 1, "minNotional": 10000})

    def test_smaller_balance_requires_revalidation(self):
        self.assertLess(self.cap(balance=1), self.cap())
