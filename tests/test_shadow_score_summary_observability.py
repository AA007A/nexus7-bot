import logging
import os
import unittest
from unittest.mock import patch

from bot.scan_summary_hardening import _ScanSummaryLabelFilter


class ShadowScoreSummaryObservabilityTests(unittest.TestCase):
    def _render(self, message):
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname=__file__, lineno=1,
            msg=message, args=(), exc_info=None,
        )
        self.assertTrue(_ScanSummaryLabelFilter().filter(record))
        return record.getMessage()

    def test_nonpaper_shadow_summary_displays_55(self):
        original = "🔎 SCAN: 3 pares | máx=54 (mín=65) | 0 a ≤5pts | 0 aprovados"
        with patch.dict(os.environ, {"PAPER_TRADE": "false"}, clear=False):
            rendered = self._render(original)
        self.assertIn("🔎 SCORE_STAGE:", rendered)
        self.assertIn("(mín=55)", rendered)
        self.assertNotIn("(mín=65)", rendered)
        self.assertIn("em faixa ≥(mín-5), incluindo ≥mín", rendered)

    def test_paper_summary_keeps_production_threshold(self):
        original = "🔎 SCAN: 3 pares | máx=64 (mín=65) | 1 a ≤5pts | 0 aprovados"
        with patch.dict(os.environ, {"PAPER_TRADE": "true"}, clear=False):
            rendered = self._render(original)
        self.assertIn("(mín=65)", rendered)
        self.assertNotIn("(mín=55)", rendered)

    def test_module_contains_no_exchange_mutation_calls(self):
        import inspect
        import bot.scan_summary_hardening as module
        source = inspect.getsource(module)
        for forbidden in (
            ".place_order(", ".cancel_order(", ".cancel_all_orders(",
            ".close_position(", ".set_leverage(", ".set_sl(",
            ".set_position_stops(",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
