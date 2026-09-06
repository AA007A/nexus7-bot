import time
from types import SimpleNamespace

from bot import news_context_hardening as hardening


def test_headline_score_signed_and_stale():
    scoring = SimpleNamespace(_news_cache={
        "timestamp": time.time(),
        "classificacao": "BULLISH",
        "score_confianca": 0.8,
    })
    assert hardening._headline_score(scoring) == (16, "RSS_HEADLINES")

    scoring._news_cache["classificacao"] = "BEARISH"
    assert hardening._headline_score(scoring) == (-16, "RSS_HEADLINES")

    scoring._news_cache["timestamp"] = time.time() - 3600
    assert hardening._headline_score(scoring) == (0, "STALE_OR_EMPTY")


def test_relevance_filter_accepts_crypto_and_macro_but_rejects_unrelated_ai():
    assert hardening._is_relevant_headline("Bitcoin ETF inflows jump after Fed decision")
    assert hardening._is_relevant_headline("Ethereum falls as CPI surprises markets")
    assert not hardening._is_relevant_headline(
        "AI Just Solved a 350-Year-Old Math Problem By Writing the Longest Proof Ever"
    )
    assert not hardening._is_relevant_headline("New smartphone launch breaks sales record")


def test_install_feeds_headline_score_into_nexus_market_sentiment():
    from bot import market_data as mdata
    from bot import score as scoring

    original_market = mdata.get_market_sentiment
    original_reader = scoring.news_reader_loop
    original_flag = getattr(scoring, "_rss_only_news_hardening", False)
    original_cache = dict(getattr(scoring, "_news_cache", {}) or {})

    class Log:
        def info(self, *args, **kwargs):
            pass
        def debug(self, *args, **kwargs):
            pass
        def warning(self, *args, **kwargs):
            pass

    try:
        if hasattr(scoring, "_rss_only_news_hardening"):
            delattr(scoring, "_rss_only_news_hardening")
        mdata.get_market_sentiment = lambda: {"score": 5, "signals": ["BASE"]}
        scoring._news_cache = {
            "timestamp": time.time(),
            "classificacao": "BEARISH",
            "score_confianca": 0.75,
        }

        hardening.install(Log())
        result = mdata.get_market_sentiment()

        assert result["score"] == -10
        assert result["headline_news_score"] == -15
        assert result["headline_news_source"] == "RSS_HEADLINES"
        assert "NEWS=-15(RSS_HEADLINES)" in result["signals"]
        assert scoring.news_reader_loop is not original_reader
    finally:
        mdata.get_market_sentiment = original_market
        scoring.news_reader_loop = original_reader
        scoring._news_cache = original_cache
        if original_flag:
            scoring._rss_only_news_hardening = True
        elif hasattr(scoring, "_rss_only_news_hardening"):
            delattr(scoring, "_rss_only_news_hardening")
