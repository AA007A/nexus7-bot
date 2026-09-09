from bot.event_intelligence import EventType, classify_event
from bot import market_risk_news_bridge


def test_us_macro_and_equity_headlines_are_classified():
    assert classify_event("Fed Powell speaks after FOMC decision", "Fed").event_type is EventType.MACRO_FOMC
    assert classify_event("Nonfarm payrolls jobs report misses expectations", "BLS").event_type is EventType.MACRO_LABOR
    assert classify_event("S&P 500 plunge sends VIX higher", "News").event_type is EventType.US_EQUITY_STRESS
    assert classify_event("US GDP growth slows sharply", "News").event_type is EventType.MACRO_GROWTH


def test_macro_news_bridge_ingests_severity_but_does_not_authorize_execution():
    class DummyNews:
        _market_risk_news_bridge_installed = False
        @staticmethod
        def _event_snapshot_for_fresh_headlines(log, headlines):
            return {
                "aggregate": {"severity": 85, "types": ["MACRO_FOMC"]},
                "decision_effect": "NONE",
                "execution_effect": "NONE",
            }

    class DummyRuntime:
        values = None
        @classmethod
        def _merge_signals(cls, values):
            cls.values = dict(values)

    class DummyLog:
        def info(self, *args, **kwargs):
            pass

    market_risk_news_bridge.install(DummyNews, DummyRuntime, DummyLog())
    snap = DummyNews._event_snapshot_for_fresh_headlines(None, [])
    assert DummyRuntime.values == {"macro_event_severity": 85}
    assert snap["execution_effect"] == "NONE"
