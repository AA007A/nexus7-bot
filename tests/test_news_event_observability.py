import unittest

from bot.news_event_observability import build_event_snapshot, compact_event_log


class NewsEventObservabilityTests(unittest.TestCase):
    def test_multi_source_event_snapshot_is_structured(self):
        snap = build_event_snapshot([
            {"title": "Fed signals rate cut as inflation cools", "source": "A"},
            {"title": "FOMC rate cut expected after CPI", "source": "B"},
            {"title": "Bitcoin ETF inflow reaches record high", "source": "C"},
        ])
        self.assertEqual(snap["event_count"], 3)
        self.assertGreaterEqual(snap["aggregate"]["consensus_sources"], 3)
        self.assertEqual(snap["decision_effect"], "NONE")
        self.assertEqual(snap["execution_effect"], "NONE")
        self.assertIn("MACRO_FOMC", snap["aggregate"]["types"])

    def test_hack_event_is_high_severity(self):
        snap = build_event_snapshot([
            {"title": "Exchange hack drains stolen funds", "source": "A"}
        ])
        self.assertGreaterEqual(snap["aggregate"]["severity"], 90)
        self.assertLessEqual(snap["aggregate"]["direction"], 0)

    def test_empty_snapshot_is_neutral(self):
        snap = build_event_snapshot([])
        self.assertEqual(snap["event_count"], 0)
        self.assertEqual(snap["aggregate"]["severity"], 0)
        self.assertEqual(snap["decision_effect"], "NONE")

    def test_log_is_explicitly_non_decisional(self):
        snap = build_event_snapshot([
            {"title": "SEC lawsuit targets crypto token", "source": "Wire"}
        ])
        line = compact_event_log(snap)
        self.assertIn("[NEWS_EVENT_INTELLIGENCE]", line)
        self.assertIn("decision_effect=NONE", line)
        self.assertIn("execution_effect=NONE", line)


if __name__ == "__main__":
    unittest.main()
