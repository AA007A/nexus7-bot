"""Public-RSS real-world news context for the NEXUS decision input.

This removes the active runtime dependency on CRYPTOPANIC_TOKEN while keeping
headline ingestion alive. Signed headline sentiment is added to the existing
market sentiment score already passed by TradingEngine to ``nexus_ai.decide``.

PAPER/LIVE mode, leverage, sizing, risk limits, execution gates and order
routing are not changed here. Structured multi-headline event intelligence is
observational only and never changes the decision score.
"""
from __future__ import annotations

import asyncio
import calendar
import re
import time

import aiohttp

_RSS_FEEDS = (
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://www.theblock.co/rss.xml",
    "https://decrypt.co/feed",
    "https://www.federalreserve.gov/feeds/press_monetary.xml",
    "https://www.bls.gov/feed/empsit.rss",
    "https://www.bls.gov/feed/cpi.rss",
    "https://www.bls.gov/feed/ppi.rss",
)

_RSS_SOURCE_NAMES = {
    "https://www.coindesk.com/arc/outboundfeeds/rss/": "CoinDesk",
    "https://cointelegraph.com/rss": "CoinTelegraph",
    "https://www.theblock.co/rss.xml": "TheBlock",
    "https://decrypt.co/feed": "Decrypt",
    "https://www.federalreserve.gov/feeds/press_monetary.xml": "FederalReserve",
    "https://www.bls.gov/feed/empsit.rss": "BLS-Employment",
    "https://www.bls.gov/feed/cpi.rss": "BLS-CPI",
    "https://www.bls.gov/feed/ppi.rss": "BLS-PPI",
}

_NEWS_TTL_SECONDS = 1800
_MAX_FUTURE_SKEW_SECONDS = 300

_RELEVANT_PATTERNS = tuple(re.compile(p, re.IGNORECASE) for p in (
    r"\bbitcoin\b", r"\bbtc\b", r"\bethereum\b", r"\beth\b",
    r"\bsolana\b", r"\bsol\b", r"\bxrp\b", r"\bdogecoin\b", r"\bdoge\b",
    r"\bcardano\b", r"\bada\b", r"\bchainlink\b", r"\blink\b",
    r"\bavalanche\b", r"\bavax\b", r"\bpolkadot\b", r"\bdot\b",
    r"\blitecoin\b", r"\bltc\b", r"\bnear\b", r"\bcosmos\b", r"\batom\b",
    r"\bcrypto(?:currency|currencies)?\b", r"\bblockchain\b", r"\bstablecoin\b",
    r"\bdefi\b", r"\btoken\b", r"\betf\b", r"\bsec\b", r"\bcftc\b",
    r"\bfomc\b", r"\bfederal reserve\b", r"\bfed\b", r"\bpowell\b",
    r"\bcpi\b", r"\bpce\b", r"\bppi\b", r"\bnfp\b", r"\bnonfarm payrolls?\b",
    r"\bjobs report\b", r"\bemployment situation\b", r"\bunemployment\b",
    r"\binflation\b", r"\binterest rate\b", r"\bgdp\b", r"\brecession\b",
    r"\btreasury\b", r"\btreasury yields?\b", r"\bdollar index\b", r"\bdxy\b",
    r"\bs&p ?500\b", r"\bspx\b", r"\bnasdaq\b", r"\bdow jones\b",
    r"\bvix\b", r"\bwall street\b", r"\bus equities\b", r"\bstock market\b",
))


def _source_name(feed_url: str) -> str:
    return _RSS_SOURCE_NAMES.get(str(feed_url), "PUBLIC_RSS")


def _event_snapshot_for_fresh_headlines(log, headlines):
    """Build and emit non-decisional structured event telemetry."""
    from bot.news_event_observability import build_event_snapshot, compact_event_log

    snapshot = build_event_snapshot(headlines)
    log.info("%s", compact_event_log(snapshot))
    return snapshot


def _is_relevant_headline(title: str) -> bool:
    text = str(title or "").strip()
    if not text:
        return False
    return any(pattern.search(text) for pattern in _RELEVANT_PATTERNS)


def _entry_published_ts(entry) -> float | None:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return None
    try:
        return float(calendar.timegm(parsed))
    except (TypeError, ValueError, OverflowError):
        return None


def _is_fresh_published_ts(published_ts: float | None, now: float | None = None) -> bool:
    if published_ts is None or published_ts <= 0:
        return False
    current = time.time() if now is None else float(now)
    age = current - float(published_ts)
    if age < -_MAX_FUTURE_SKEW_SECONDS:
        return False
    return age <= _NEWS_TTL_SECONDS


def _headline_score(scoring) -> tuple[int, str]:
    cache = getattr(scoring, "_news_cache", {}) or {}
    fetched_ts = float(cache.get("timestamp", 0) or 0)
    published_ts = float(cache.get("published_at", 0) or 0)
    now = time.time()
    if fetched_ts <= 0 or now - fetched_ts > _NEWS_TTL_SECONDS:
        return 0, "STALE_OR_EMPTY"
    if not _is_fresh_published_ts(published_ts, now):
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
        """Refresh fresh, public, market-relevant headline sentiment every two minutes."""
        log.info(
            "[NEWS_CONTEXT] public RSS enabled; no CryptoPanic token required; "
            "sources=CoinDesk,CoinTelegraph,TheBlock,Decrypt,FederalReserve,BLS"
        )
        while True:
            try:
                async with aiohttp.ClientSession() as session:
                    best = None
                    seen = 0
                    relevant = 0
                    fresh = 0
                    fresh_headlines = []
                    dedupe = set()
                    now = time.time()
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
                                dedupe_key = re.sub(r"\s+", " ", title.casefold()).strip()
                                if dedupe_key in dedupe:
                                    continue
                                dedupe.add(dedupe_key)
                                seen += 1
                                if not _is_relevant_headline(title):
                                    continue
                                relevant += 1
                                published_ts = _entry_published_ts(entry)
                                if not _is_fresh_published_ts(published_ts, now):
                                    continue
                                fresh += 1
                                fresh_headlines.append({
                                    "title": title,
                                    "source": _source_name(feed_url),
                                })
                                classification, confidence, is_fomc = scoring._classify_news(title)
                                candidate = (
                                    float(confidence), float(published_ts), classification, is_fomc, title
                                )
                                if best is None or candidate[:2] > best[:2]:
                                    best = candidate
                        except Exception as exc:
                            log.debug(
                                "[NEWS_CONTEXT] RSS unavailable url=%s error=%s",
                                feed_url,
                                type(exc).__name__,
                            )
                    if best is not None:
                        confidence, published_ts, classification, is_fomc, title = best
                        impact = 15 if confidence >= 0.8 else 5
                        scoring._news_cache.update({
                            "classificacao": classification,
                            "score_confianca": confidence,
                            "impacto": impact,
                            "timestamp": time.time(),
                            "published_at": published_ts,
                            "fomc_window": is_fomc,
                            "source": "PUBLIC_RSS",
                            "headline": title[:180],
                        })
                        age_seconds = max(0, int(time.time() - published_ts))
                        log.info(
                            "[NEWS_CONTEXT] headline sentiment=%s confidence=%.2f "
                            "age=%ss fomc=%s title=%s",
                            classification,
                            confidence,
                            age_seconds,
                            is_fomc,
                            title[:100],
                        )
                    else:
                        log.info(
                            "[NEWS_CONTEXT] no fresh relevant RSS headline; seen=%s relevant=%s fresh=%s; "
                            "headline contribution remains neutral",
                            seen,
                            relevant,
                            fresh,
                        )
                        scoring._news_cache.update({
                            "classificacao": "NEUTRO",
                            "score_confianca": 0.0,
                            "impacto": 0,
                            "timestamp": time.time(),
                            "published_at": time.time(),
                            "fomc_window": False,
                            "source": "PUBLIC_RSS",
                            "headline": "",
                        })

                    # Multi-headline event intelligence is telemetry only. Any
                    # failure here is isolated from the existing headline score.
                    try:
                        _event_snapshot_for_fresh_headlines(log, fresh_headlines)
                    except Exception as exc:
                        log.warning(
                            "[NEWS_EVENT_INTELLIGENCE] telemetry_failed error=%s "
                            "decision_effect=NONE execution_effect=NONE",
                            type(exc).__name__,
                        )
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
        "[NEWS_CONTEXT] installed: crypto + official US macro RSS feeds feed NEXUS market sentiment; "
        "structured multi-headline event telemetry enabled; CryptoPanic token removed from active path"
    )
