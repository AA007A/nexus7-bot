"""Pretrade must not synthesize candles when market data is unavailable."""
import ast
from pathlib import Path
import unittest


class PretradeFailClosedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine_path = Path(__file__).resolve().parents[1] / "bot/engine.py"
        cls.tree = ast.parse(cls.engine_path.read_text(encoding="utf-8"))

    def test_open_has_no_synthetic_entry_price_candle_series(self):
        synthetic = []
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.BinOp) or not isinstance(node.op, ast.Mult):
                continue
            if not isinstance(node.left, ast.List) or len(node.left.elts) != 1:
                continue
            value = node.left.elts[0]
            if (isinstance(value, ast.Attribute)
                    and isinstance(value.value, ast.Name)
                    and value.value.id == "sig"
                    and value.attr == "entry"):
                synthetic.append(node)
        self.assertEqual(synthetic, [])

    def test_pretrade_requires_twenty_closed_plus_current_candle(self):
        source = self.engine_path.read_text(encoding="utf-8")
        self.assertIn("if len(kl) <= 20:", source)
        self.assertIn("dados insuficientes", source)


if __name__ == "__main__":
    unittest.main()
