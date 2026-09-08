import logging
import os
import unittest

from bot import shadow_startup_logging as ssl


class ShadowStartupLoggingTests(unittest.TestCase):
    def setUp(self):
        self.old_env = os.environ.get("PAPER_TRADE")
        self.old_factory = logging.getLogRecordFactory()
        os.environ["PAPER_TRADE"] = "false"
        ssl._INSTALLED = False
        ssl._ORIGINAL_FACTORY = None
        ssl.install_preimport()

    def tearDown(self):
        logging.setLogRecordFactory(self.old_factory)
        ssl._INSTALLED = False
        ssl._ORIGINAL_FACTORY = None
        if self.old_env is None:
            os.environ.pop("PAPER_TRADE", None)
        else:
            os.environ["PAPER_TRADE"] = self.old_env

    def _record(self, level, message):
        return logging.getLogRecordFactory()(
            "test", level, __file__, 1, message, (), None
        )

    def test_shadow_live_banner_is_warning(self):
        rec = self._record(logging.CRITICAL, "OPERAÇÃO REAL ATIVA")
        self.assertEqual(rec.levelno, logging.WARNING)
        self.assertIn("SHADOW LIVE ATIVO", rec.getMessage())

    def test_cosmetic_separator_is_not_critical(self):
        rec = self._record(logging.CRITICAL, "=" * 60)
        self.assertEqual(rec.levelno, logging.WARNING)

    def test_genuine_critical_remains_critical(self):
        rec = self._record(logging.CRITICAL, "exchange mutation guard failed")
        self.assertEqual(rec.levelno, logging.CRITICAL)
        self.assertEqual(rec.getMessage(), "exchange mutation guard failed")


if __name__ == "__main__":
    unittest.main()
