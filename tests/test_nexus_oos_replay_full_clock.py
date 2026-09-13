import unittest

from bot import market_data_integrity, nexus_ai
from bot.nexus_oos_real_replay_corrected import _freeze_full_clock


class TestNexusOOSReplayFullClock(unittest.TestCase):
    def test_freezes_and_restores_both_clock_owners(self):
        old_nexus = nexus_ai.time.time
        old_integrity = market_data_integrity.time.time
        decision_ts_ms = 1_700_000_000_000
        with _freeze_full_clock(nexus_ai, decision_ts_ms):
            expected = decision_ts_ms / 1000.0 + 1.0
            self.assertEqual(nexus_ai.time.time(), expected)
            self.assertEqual(market_data_integrity.time.time(), expected)
        self.assertIs(nexus_ai.time.time, old_nexus)
        self.assertIs(market_data_integrity.time.time, old_integrity)


if __name__ == "__main__":
    unittest.main()
