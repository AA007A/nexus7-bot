import unittest
from unittest.mock import patch

from bot.daily_tracker import DailyTracker


class DailyStopPreliveTests(unittest.TestCase):
    def test_recalc_uses_configured_percentage(self):
        tracker = DailyTracker()
        with patch("bot.daily_tracker.cfg.DAILY_STOP_LOSS", 0.0), patch(
            "bot.daily_tracker.cfg.DAILY_STOP_LOSS_PCT", 0.03
        ):
            tracker.recalc_limits(100.0)
        self.assertEqual(tracker.daily_stop_loss, 3.0)

    def test_daily_stop_blocks_new_entries(self):
        tracker = DailyTracker()
        tracker.daily_stop_loss = 3.0
        tracker.daily_pnl = -3.0
        self.assertEqual(tracker.check_limits(), "STOP")
        self.assertTrue(tracker.daily_stopped)
        self.assertFalse(tracker.can_trade())
        self.assertEqual(tracker.to_dict()["mode"], "PARADO_DIA")

    def test_daily_reset_releases_daily_stop(self):
        tracker = DailyTracker()
        tracker.daily_stopped = True
        tracker._last_reset_day = -1
        with patch("bot.daily_tracker.cfg.DAILY_STOP_LOSS", 0.0), patch(
            "bot.daily_tracker.cfg.DAILY_STOP_LOSS_PCT", 0.03
        ):
            tracker.check_reset(100.0)
        self.assertFalse(tracker.daily_stopped)
        self.assertTrue(tracker.can_trade())


if __name__ == "__main__":
    unittest.main()
