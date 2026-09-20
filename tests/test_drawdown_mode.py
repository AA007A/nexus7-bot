import importlib
import os
import unittest
from unittest.mock import patch


class DrawdownModeConfigTests(unittest.TestCase):
    def _reload(self):
        import bot.config as config
        return importlib.reload(config)

    def test_default_is_advisory(self):
        env = dict(os.environ)
        env.pop("DRAWDOWN_MODE", None)
        with patch.dict(os.environ, env, clear=True):
            config = self._reload()
            self.assertEqual(config.cfg.DRAWDOWN_MODE, "ADVISORY")

    def test_hard_gate_is_explicit(self):
        with patch.dict(os.environ, {"DRAWDOWN_MODE": "HARD_GATE"}, clear=False):
            config = self._reload()
            self.assertEqual(config.cfg.DRAWDOWN_MODE, "HARD_GATE")

    def test_invalid_mode_fails_closed_at_config_load(self):
        with patch.dict(os.environ, {"DRAWDOWN_MODE": "MAGIC"}, clear=False):
            with self.assertRaises(ValueError):
                self._reload()


if __name__ == "__main__":
    unittest.main()
