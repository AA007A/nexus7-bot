import pathlib
import unittest

from bot import rr_precision_hardening as rrh
from bot.config import cfg


class RRPrecisionIsolationTests(unittest.TestCase):
    def test_proxy_overrides_only_rr_threshold(self):
        configured = float(cfg.MIN_RR_RATIO)
        proxy = rrh._CfgProxy(cfg, configured - rrh._RR_EPSILON)

        self.assertAlmostEqual(proxy.MIN_RR_RATIO, configured - rrh._RR_EPSILON)
        self.assertEqual(proxy.LEVERAGE, cfg.LEVERAGE)
        self.assertAlmostEqual(float(cfg.MIN_RR_RATIO), configured)

    def test_proxy_is_read_only(self):
        proxy = rrh._CfgProxy(cfg, float(cfg.MIN_RR_RATIO))
        with self.assertRaises(AttributeError):
            proxy.MIN_RR_RATIO = 1.0

    def test_hardening_does_not_assign_shared_threshold(self):
        source = pathlib.Path("bot/rr_precision_hardening.py").read_text(encoding="utf-8")
        self.assertNotIn("cfg.MIN_RR_RATIO =", source)
        self.assertNotIn("original_cfg.MIN_RR_RATIO =", source)
        self.assertIn("strategy.cfg = _CfgProxy", source)
        self.assertIn("strategy.cfg = original_cfg", source)


if __name__ == "__main__":
    unittest.main()
