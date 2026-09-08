import unittest

from bot.news_context_hardening import (
    _event_snapshot_for_fresh_headlines,
    _source_name,
)


class _Log:
    def __init__(self):
        self.messages = []

    def info(self, fmt, *args):
        self.messages.append(fmt % args if args else fmt)


class NewsEventRuntimeWiringTests(unittest.TestCase):
    def test_source_name_is_stable(self):
        self.assertEqual(
            _source_name("https://cointelegraph.com/rss"),
            "CoinTelegraph",
        )
        self.assertEqual(_source_name("https://example.invalid/rss"), "PUBLIC_RSS")

    def test_multi_headline_snapshot_is_observational_only(self):
        log = _Log()
        snapshot = _event_snapshot_for_fresh_headlines(
            log,
            [
                {"title": "SEC approves spot Bitcoin ETF filing", "source": "CoinDesk"},
                {"title": "Major crypto exchange reports exploit", "source": "Decrypt"},
            ],
        )
        self.assertEqual(snapshot["decision_effect"], "NONE")
        self.assertEqual(snapshot["execution_effect"], "NONE")
        self.assertEqual(snapshot["event_count"], 2)
        self.assertTrue(log.messages)
        self.assertIn("[NEWS_EVENT_INTELLIGENCE]", log.messages[-1])
        self.assertIn("decision_effect=NONE", log.messages[-1])
        self.assertIn("execution_effect=NONE", log.messages[-1])

    def test_empty_snapshot_remains_neutral_and_safe(self):
        log = _Log()
        snapshot = _event_snapshot_for_fresh_headlines(log, [])
        self.assertEqual(snapshot["event_count"], 0)
        self.assertEqual(snapshot["decision_effect"], "NONE")
        self.assertEqual(snapshot["execution_effect"], "NONE")
        self.assertIn("events=0", log.messages[-1])


if __name__ == "__main__":
    unittest.main()
