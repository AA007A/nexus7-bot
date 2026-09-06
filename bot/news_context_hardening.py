"""Public-RSS real-world news context for the NEXUS decision input.

This removes the active runtime dependency on CRYPTOPANIC_TOKEN while keeping
headline ingestion alive. Signed headline sentiment is added to the existing
market sentiment score already passed by TradingEngine to ``nexus_ai.decide``.

PAPER/LIVE mode, leverage, sizing, risk limits, execution gates and order
routing are not changed here.
"""
from __future__ import annotations

import asyncio
import re
import time

import aiohttp

_RSS_FEEDS = (
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://www.theblock.co/rss.xml",
    "https://decrypt.co/feed",
)

_RELEVANT_PATTERNS = tuple(re.compile(p, re.IGNORECASE) for p in (
    r"\bbitcoin\b", r"\bbtc\b", r"\bethereum\b", r"\beth\b",
    r"\bsolana\b", r"\bsol\b", r"\bxrp\b", r"\bdogecoin\b", r"\bdoge\b",
    r"\bcardano\b", r"\bada\b", r"\bchainlink\b", r"\blink\b",
    r"\bavalanche\b", r"\bavax\b", r"\bpolkadot\b", r"\bdot\b",
    r"\blitecoin\b", r"\bltc\b", r"\bnear\b", r"\bcosmos\b", r"\batom\b",
    r"\bcrypto(?:currency|currencies)?\b", r"\bblockchain\b", r"\bstablecoin\b",
    r"\bdefi\b", r"\btoken\b", r"\betf\b", r"\bsec\b", r"\bcftc\b",
    r"\bfomc\b", r"\bfederal reserve\b", r"\bfed\b", r"\bpowell\b",
    r"\bcpi\b", r"\bpce\b", r"\bnfp\b", r"\binflation\b", r"\binterest rate\b",
    r"\btreasury\b", r"\bdollar index\b", r"\bdxy\b",
))


def _is_relevant_headline(title: str) -> bool:
    text = str(title or "").strip()
    if not text:
        return False
    return any(pattern.search(text) for pattern in _RELEVANT_PATTERNS)


def _headline_score(scoring) -> tuple[int, str]:
    cache = getattr(scoring, "_news_cache", {}) or {}
    ts = float(cache.get("timestamp", 0) or 0)
    if ts <= 0 or time.time() - ts > 1800:
        return 0, "STALE_OR_EMPTY"
    classification = str(cache.get("classificacao", "NEUTRO")).upper()
    confidence = max(0.0, min(1.0, float(cache.get("score_confianca", 0) or 0)))
    magnitude = int(round(confidence * 20))
    if classification == "BULLISH":
        return magnitude, "RSS_HEADLINES"
    if classification == "BEARISH":
        return -magnitude, "RSS_HEADLINES"
    return 0, "RSS_HEADLINES"


def install(log):
    from bot import market_data as mdata
    from bot import score as scoring

    if getattr(scoring, "_rss_only_news_hardening", False):
        return

    async def rss_news_reader_loop():
        """Refresh public, market-relevant headline sentiment every two minutes."""
        log.info(
            "[NEWS_CONTEXT] public RSS enabled; no CryptoPanic token required; "
            "sources=CoinDesk,CoinTelegraph,TheBlock,Decrypt"
        )
        while True:
            try:
                async with aiohttp.ClientSession() as session:
                    best = None
                    seen = 0
                    relevant = 0
                    for feed_url in _RSS_FEEDS:
                        try:
                            async with session.get(
                                feed_url,
                                timeout=aiohttp.ClientTimeout(total=8),
                                headers={"User-Agent": "BGX-Capital/12.1"},
                            ) as response:
                                if response.status != 200:
                                    continue
                                content = await response.text()
                            import feedparser
                            feed = feedparser.parse(content)
                            for entry in feed.entries[:8]:
                                title = str(entry.get("title", "") or "").strip()
                                if not title:
                                    continue
                                seen += 1
                                if not _is_relevant_headline(title):
                                    continue
                                relevant += 1
                                classification, confidence, is_fomc = scoring._classify_news(title)
                                candidate = (
                                    float(confidence), classification, is_fomc, title
                                )
                                if best is None or candidate[0] > best[0]:
                                    best = candidate
                        except Exception as exc:
                            log.debug(
                                "[NEWS_CONTEXT] RSS unavailable url=%s error=%s",
                                feed_url,
                                type(exc).__name__,
                            )
                    if best is not None:
                        confidence, classification, is_fomc, title = best
                        impact = 15 if confidence >= 0.8 else 5
                        scoring._news_cache.update({
                            "classificacao": classification,
                            "score_confianca": confidence,
                            "impacto": impact,
                            "timestamp": time.time(),
                            "fomc_window": is_fomc,
                            "source": "PUBLIC_RSS",
                            "headline": title[:180],
                        })
                        log.info(
                            "[NEWS_CONTEXT] headline sentiment=%s confidence=%.2f "
                            "fomc=%s title=%s",
                            classification,
                            confidence,
                            is_fomc,
                            title[:100],
                        )
                    else:
                        log.info(
                            "[NEWS_CONTEXT] no relevant RSS headline; seen=%s relevant=%s; "
                            "headline contribution remains neutral",
                            seen,
                            relevant,
                        )
                        scoring._news_cache.update({
                            "classificacao": "NEUTRO",
                            "score_confianca": 0.0,
                            "impacto": 0,
                            "timestamp": time.time(),
                            "fomc_window": False,
                            "source": "PUBLIC_RSS",
                            "headline": "",
                        })
            except Exception as exc:
                log.warning(
                    "[NEWS_CONTEXT] RSS refresh failed: %s: %s",
                    type(exc).__name__, exc,
                )
            await asyncio.sleep(120)

    original_market_sentiment = mdata.get_market_sentiment

    def market_sentiment_with_headlines():
        base = original_market_sentiment()
        out = dict(base) if isinstance(base, dict) else {"score": 0, "signals": []}
        base_score = float(out.get("score", 0) or 0)
        news_score, source = _headline_score(scoring)
        combined = max(-100.0, min(100.0, base_score + news_score))
        signals = list(out.get("signals", []) or [])
        signals.append(f"NEWS={news_score:+d}({source})")
        out.update({
            "score": combined,
            "signals": signals,
            "headline_news_score": news_score,
            "headline_news_source": source,
        })
        return out

    scoring.news_reader_loop = rss_news_reader_loop
    mdata.get_market_sentiment = market_sentiment_with_headlines
    scoring._rss_only_news_hardening = True
    log.info(
        "[NEWS_CONTEXT] installed: NEXUS market sentiment includes relevant public RSS "
        "headline score; CryptoPanic token removed from active news path"
    )
