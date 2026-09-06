"""Regression guards for PAPER signal-driven exits.

A PAPER reduceOnly order is intentionally only a synthetic acknowledgement.
Signal-driven full exits therefore must settle the internal PAPER position
through the PAPER lifecycle, not through KuCoinClient.place_order().
"""
import inspect
import unittest

from bot import paper_lifecycle


class PaperSignalExitRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = inspect.getsource(paper_lifecycle.install)

    def test_paper_signal_exit_is_installed(self):
        self.assertIn(
            "original_exit_check = TradingEngine._check_stagnation_and_invalidation",
            self.source,
        )
        self.assertIn(
            "TradingEngine._check_stagnation_and_invalidation = _paper_exit_check",
            self.source,
        )

    def test_live_path_delegates_to_original_implementation(self):
        section = self.source.split("async def _paper_exit_check", 1)[1].split(
            "async def paper_sync", 1
        )[0]
        self.assertIn("if not self.paper_trade:", section)
        self.assertIn("return await original_exit_check(self)", section)

    def test_paper_signal_exits_use_internal_atomic_settlement(self):
        section = self.source.split("async def _paper_exit_check", 1)[1].split(
            "async def paper_sync", 1
        )[0]
        self.assertIn(
            'await _finish_paper_position(self, sym, pos, cur, "TIME")', section
        )
        self.assertIn(
            'await _finish_paper_position(\n                            self, sym, pos, cur, "INVALIDATION"',
            section,
        )
        self.assertIn(
            'await _finish_paper_position(\n                            self, sym, pos, cur, "REGIME"',
            section,
        )
        # Regression that caused repeated `[PAPER] Sell ...` every scan:
        # the PAPER path must never use the exchange/synthetic reduceOnly route.
        self.assertNotIn(".place_order(", section)

    def test_paper_close_has_per_symbol_inflight_guard(self):
        section = self.source.split("async def _finish_paper_position", 1)[1].split(
            "async def _paper_exit_check", 1
        )[0]
        self.assertIn("_paper_closing_symbols", section)
        self.assertIn("if sym in closing:", section)
        self.assertIn("closing.add(sym)", section)
        self.assertIn("finally:", section)
        self.assertIn("closing.discard(sym)", section)


if __name__ == "__main__":
    unittest.main()
