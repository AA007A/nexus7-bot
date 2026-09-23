import unittest

from bot.runtime_truth_exporter import _split_by_utc_day


class RuntimeTruthExporterTests(unittest.TestCase):
    def test_batches_split_at_utc_day_without_reordering(self):
        events = [
            {"timestamp_utc":"2026-09-23T23:59:59.900000Z","monotonic_sequence":1},
            {"timestamp_utc":"2026-09-23T23:59:59.999999Z","monotonic_sequence":2},
            {"timestamp_utc":"2026-09-24T00:00:00.000001Z","monotonic_sequence":3},
            {"timestamp_utc":"2026-09-24T00:00:01Z","monotonic_sequence":4},
        ]
        groups = _split_by_utc_day(events)
        self.assertEqual([[e["monotonic_sequence"] for e in g] for g in groups], [[1,2],[3,4]])


if __name__ == "__main__":
    unittest.main()
