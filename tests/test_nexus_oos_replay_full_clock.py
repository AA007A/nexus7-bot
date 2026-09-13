import unittest

from bot import market_data_integrity, nexus_ai
from bot.nexus_oos_real_replay_corrected import _freeze_full_clock, fetch_history_contiguous


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


class TestNexusOOSHistoryPagination(unittest.IsolatedAsyncioTestCase):
    async def test_request_window_never_exceeds_200_futures_candles(self):
        class Client:
            def __init__(self):
                self.calls = []

            async def _get(self, path, params=None, auth=False):
                self.calls.append((path, dict(params or {})))
                return []

        client = Client()
        result = await fetch_history_contiguous(client, "BTCUSDT", "15", 450)
        self.assertEqual(result, [])
        self.assertEqual(len(client.calls), 1)
        _, params = client.calls[0]
        span_ms = int(params["to"]) - int(params["from"])
        self.assertLessEqual(span_ms, 200 * 15 * 60 * 1000)


if __name__ == "__main__":
    unittest.main()
