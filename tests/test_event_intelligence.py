import unittest

from bot.event_intelligence import EventType, aggregate_events, classify_event


class EventIntelligenceTests(unittest.TestCase):
    def test_hack_is_high_severity_bearish(self):
        e = classify_event("Major protocol hack drains funds", "SourceA")
        self.assertEqual(e.event_type, EventType.HACK_EXPLOIT)
        self.assertEqual(e.direction, -1)
        self.assertGreaterEqual(e.severity, 90)

    def test_fomc_is_structured_macro_event(self):
        e = classify_event("Fed Powell speaks after FOMC decision", "SourceA")
        self.assertEqual(e.event_type, EventType.MACRO_FOMC)
        self.assertGreaterEqual(e.severity, 80)

    def test_asset_specific_event_extracts_symbol(self):
        e = classify_event("SOL adoption launch expands institutional access", "SourceA")
        self.assertIn("SOL", e.assets)

    def test_multi_source_consensus_increases_severity(self):
        events = [
            classify_event("SEC lawsuit targets crypto platform", "A"),
            classify_event("SEC enforcement lawsuit expands", "B"),
            classify_event("Regulation lawsuit shakes crypto market", "C"),
        ]
        agg = aggregate_events(events)
        self.assertEqual(agg["consensus_sources"], 3)
        self.assertGreaterEqual(agg["severity"], 80)
        self.assertLess(agg["direction"], 0)


if __name__ == "__main__":
    unittest.main()
