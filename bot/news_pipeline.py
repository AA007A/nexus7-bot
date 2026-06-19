import feedparser
"""
BGX Capital — News Intelligence Pipeline v1.0
Pipeline unificado: RSS Premium + Twitter + CryptoPanic
com NLP, scoring de relevância e deduplicação.
"""
import asyncio, time, hashlib, os
import aiohttp
from dataclasses import dataclass, field
from typing import Optional
from bot.logger import log


@dataclass
class NewsItem:
    title:       str
    source:      str
    source_tier: int
    url:         str   = ""
    timestamp:   float = field(default_factory=time.time)
    sentiment:   str   = "NEUTRAL"
    confidence:  float = 0.0
    impact:      float = 0.0
    relevance:   int   = 0
    entities:    list  = field(default_factory=list)
    raw_score:   int   = 0
    uid:         str   = ""

    def __post_init__(self):
        self.uid = hashlib.md5(self.title.lower()[:60].encode()).hexdigest()[:12]


SOURCE_WEIGHTS = {
    "CoinDesk":        {"tier": 1, "weight": 1.0},
    "CoinTelegraph":   {"tier": 1, "weight": 0.95},
    "The Block":       {"tier": 1, "weight": 0.95},
    "Decrypt":         {"tier": 1, "weight": 0.85},
    "Bitcoin Magazine":{"tier": 1, "weight": 0.85},
    "Blockworks":      {"tier": 1, "weight": 0.85},
    "CryptoPanic":     {"tier": 2, "weight": 0.75},
    "CryptoCompare":   {"tier": 2, "weight": 0.70},
    "Twitter":         {"tier": 3, "weight": 0.55},
    "Nitter":          {"tier": 3, "weight": 0.50},
}

BULLISH_LEXICON = {
    "all-time high": 1.0, "ath": 1.0, "breakout": 0.9, "surge": 0.85,
    "rally": 0.85, "pump": 0.8, "moon": 0.75, "bullish": 0.9,
    "buy": 0.7, "long": 0.7, "accumulate": 0.8, "institutional": 0.75,
    "etf approved": 1.0, "sec approved": 1.0, "adoption": 0.8,
    "partnership": 0.7, "upgrade": 0.7, "launch": 0.65, "listed": 0.8,
    "halving": 0.85, "record": 0.75, "inflow": 0.8, "bull run": 0.9,
    "golden cross": 0.85, "support held": 0.75, "higher high": 0.8,
    "spot etf": 0.95, "blackrock": 0.85, "fidelity": 0.8,
}

BEARISH_LEXICON = {
    "crash": 1.0, "collapse": 1.0, "dump": 0.9, "bear": 0.85,
    "sell": 0.7, "short": 0.7, "decline": 0.75, "drop": 0.75,
    "fear": 0.7, "panic": 0.85, "liquidation": 0.9, "rekt": 0.85,
    "ban": 0.95, "crackdown": 0.9, "hack": 0.9, "exploit": 0.9,
    "scam": 0.85, "fraud": 0.9, "sec lawsuit": 1.0,
    "death cross": 0.85, "lower low": 0.8,
    "rate hike": 0.8, "outflow": 0.75, "capitulation": 0.9,
}

CRYPTO_ENTITIES = [
    "BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "DOGE", "LINK",
    "AVAX", "MATIC", "DOT", "LTC", "Bitcoin", "Ethereum", "Solana",
    "crypto", "DeFi", "blockchain",
]

HIGH_IMPACT_ENTITIES = {
    "BTC", "Bitcoin", "ETH", "Ethereum", "Fed",
    "SEC", "BlackRock", "Fidelity", "CFTC", "Treasury"
}

RSS_SOURCES = [
    {"name": "CoinDesk",         "url": "https://www.coindesk.com/arc/outboundfeeds/rss/",  "tier": 1},
    {"name": "CoinTelegraph",    "url": "https://cointelegraph.com/rss",                    "tier": 1},
    {"name": "The Block",        "url": "https://www.theblock.co/rss.xml",                  "tier": 1},
    {"name": "Decrypt",          "url": "https://decrypt.co/feed",                          "tier": 1},
    {"name": "Bitcoin Magazine", "url": "https://bitcoinmagazine.com/.rss/full/",           "tier": 1},
    {"name": "Blockworks",       "url": "https://blockworks.co/feed",                       "tier": 1},
]

TWITTER_ACCOUNTS  = ["WhalePanda", "DocumentingBTC", "CryptoCobain", "inversebrah"]
TWITTER_SEARCHES  = ["bitcoin+bullish+OR+bearish", "crypto+pump+OR+dump+OR+crash"]
NITTER_INSTANCES  = ["nitter.net", "nitter.poast.org", "nitter.privacydev.net"]


def classify_text(text: str) -> tuple:
    text_lower = text.lower()
    bull = sum(w for p, w in BULLISH_LEXICON.items() if p in text_lower)
    bear = sum(w for p, w in BEARISH_LEXICON.items() if p in text_lower)
    entities = [e for e in CRYPTO_ENTITIES if e.lower() in text_lower]
    has_hi   = any(e in text for e in HIGH_IMPACT_ENTITIES)
    total    = bull + bear
    if total == 0:
        return "NEUTRAL", 0.0, 0.0, entities
    br  = bull / total
    conf = min(1.0, total / 3.0) * (1.3 if has_hi else 1.0)
    conf = min(1.0, conf)
    if br >= 0.65:
        return "BULLISH", round(conf, 3), round(br * conf, 3), entities
    elif br <= 0.35:
        return "BEARISH", round(conf, 3), round(-(1 - br) * conf, 3), entities
    return "NEUTRAL", round(conf * 0.5, 3), 0.0, entities


def calc_relevance(item: "NewsItem") -> int:
    score = 0
    age = (time.time() - item.timestamp) / 60
    if age < 15:    score += 40
    elif age < 30:  score += 35
    elif age < 60:  score += 25
    elif age < 120: score += 15
    elif age < 240: score += 5
    score += {1: 30, 2: 20, 3: 10}.get(item.source_tier, 5)
    score += int(item.confidence * 20)
    if any(e in item.entities for e in HIGH_IMPACT_ENTITIES):
        score += 10
    return min(100, score)


def deduplicate(items: list, threshold: float = 0.55) -> list:
    def jaccard(a, b):
        sa, sb = set(a.lower().split()), set(b.lower().split())
        return len(sa & sb) / len(sa | sb) if sa and sb else 0.0
    seen = []
    for item in sorted(items, key=lambda x: x.relevance, reverse=True):
        if not any(jaccard(item.title, s.title) >= threshold for s in seen):
            seen.append(item)
    return seen


async def _fetch_rss(session, source, max_items=5):
    items = []
    try:
        async with session.get(
            source["url"], timeout=aiohttp.ClientTimeout(total=8),
            headers={"User-Agent": "BGX-Capital/1.0"}
        ) as r:
            if r.status != 200:
                return []
            text = await r.text()
        feed = feedparser.parse(text)
        for entry in feed.entries[:max_items]:
            title = entry.get("title", "").strip()
            if not title or len(title) < 10:
                continue
            pub = entry.get("published_parsed") or entry.get("updated_parsed")
            ts  = time.mktime(pub) if pub else time.time()
            body = entry.get("summary", "") + " " + title
            sent, conf, imp, ents = classify_text(body)
            item = NewsItem(
                title=title, source=source["name"], source_tier=source["tier"],
                url=entry.get("link", ""), timestamp=ts,
                sentiment=sent, confidence=conf, impact=imp, entities=ents,
            )
            item.relevance = calc_relevance(item)
            items.append(item)
    except Exception as e:
        log.debug(f"RSS {source['name']}: {e}")
    return items


async def _fetch_twitter_nitter(session) -> list:
    items = []
    seen_urls = []
    for account in TWITTER_ACCOUNTS[:4]:
        for instance in NITTER_INSTANCES:
            try:
                async with session.get(
                    f"https://{instance}/{account}/rss",
                    timeout=aiohttp.ClientTimeout(total=5),
                    headers={"User-Agent": "Mozilla/5.0"}
                ) as r:
                    if r.status != 200:
                        continue
                    text = await r.text()
                feed = feedparser.parse(text)
                for entry in feed.entries[:3]:
                    title = entry.get("title", "").strip()
                    link  = entry.get("link", "")
                    if not title or link in seen_urls:
                        continue
                    seen_urls.append(link)
                    sent, conf, imp, ents = classify_text(title)
                    if sent == "NEUTRAL" and conf < 0.2:
                        continue
                    item = NewsItem(
                        title=f"@{account}: {title[:120]}",
                        source="Twitter", source_tier=3, url=link,
                        timestamp=time.time(), sentiment=sent,
                        confidence=conf, impact=imp, entities=ents,
                    )
                    item.relevance = calc_relevance(item)
                    items.append(item)
                break
            except Exception:
                continue
    for query in TWITTER_SEARCHES[:2]:
        for instance in NITTER_INSTANCES:
            try:
                async with session.get(
                    f"https://{instance}/search/rss?q={query}&f=tweets",
                    timeout=aiohttp.ClientTimeout(total=5),
                    headers={"User-Agent": "Mozilla/5.0"}
                ) as r:
                    if r.status != 200:
                        continue
                    text = await r.text()
                feed = feedparser.parse(text)
                for entry in feed.entries[:5]:
                    title = entry.get("title", "").strip()
                    if not title:
                        continue
                    sent, conf, imp, ents = classify_text(title)
                    if sent == "NEUTRAL":
                        continue
                    item = NewsItem(
                        title=title[:140], source="Nitter", source_tier=3,
                        url=entry.get("link", ""), timestamp=time.time(),
                        sentiment=sent, confidence=conf, impact=imp, entities=ents,
                    )
                    item.relevance = calc_relevance(item)
                    items.append(item)
                break
            except Exception:
                continue
    return items


async def _fetch_cryptopanic(session) -> list:
    items = []
    token = os.environ.get("CRYPTOPANIC_TOKEN", "")
    try:
        if token:
            url = (f"https://cryptopanic.com/api/v1/posts/"
                   f"?auth_token={token}&filter=hot&public=true"
                   f"&kind=news&currencies=BTC,ETH,SOL,BNB")
        else:
            url = "https://cryptopanic.com/news/rss/"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
            if r.status != 200:
                return []
            if token:
                data = await r.json()
                for post in data.get("results", [])[:10]:
                    title  = post.get("title", "")
                    votes  = post.get("votes", {})
                    net    = votes.get("positive", 0) - votes.get("negative", 0)
                    sent, conf, imp, ents = classify_text(title)
                    if net > 5:  imp = min(1.0, imp + 0.2)
                    if net < -5: imp = max(-1.0, imp - 0.2)
                    item = NewsItem(
                        title=title, source="CryptoPanic", source_tier=2,
                        url=post.get("url", ""), timestamp=time.time(),
                        sentiment=sent, confidence=conf, impact=imp,
                        entities=ents, raw_score=net,
                    )
                    item.relevance = calc_relevance(item)
                    items.append(item)
            else:
                feed = feedparser.parse(await r.text())
                for entry in feed.entries[:8]:
                    title = entry.get("title", "").strip()
                    if not title:
                        continue
                    sent, conf, imp, ents = classify_text(title)
                    item = NewsItem(
                        title=title, source="CryptoPanic", source_tier=2,
                        timestamp=time.time(), sentiment=sent,
                        confidence=conf, impact=imp, entities=ents,
                    )
                    item.relevance = calc_relevance(item)
                    items.append(item)
    except Exception as e:
        log.debug(f"CryptoPanic: {e}")
    return items


_pipeline_cache:    list  = []
_pipeline_last_run: float = 0.0
_PIPELINE_INTERVAL: int   = 300   # 5 minutos


async def run_news_pipeline():
    """Executa pipeline completo em paralelo e atualiza cache."""
    global _pipeline_cache, _pipeline_last_run
    if time.time() - _pipeline_last_run < _PIPELINE_INTERVAL:
        return
    log.info("📰 News pipeline iniciando...")
    start = time.time()
    try:
        async with aiohttp.ClientSession() as session:
            tasks   = [_fetch_rss(session, src) for src in RSS_SOURCES]
            tasks  += [_fetch_twitter_nitter(session), _fetch_cryptopanic(session)]
            results = await asyncio.gather(*tasks, return_exceptions=True)
        all_items = []
        for r in results:
            if isinstance(r, list):
                all_items.extend(r)
        unique            = deduplicate(all_items, threshold=0.55)
        unique.sort(key=lambda x: x.relevance, reverse=True)
        _pipeline_cache   = unique[:50]
        _pipeline_last_run = time.time()
        bull = sum(1 for i in _pipeline_cache if i.sentiment == "BULLISH")
        bear = sum(1 for i in _pipeline_cache if i.sentiment == "BEARISH")
        log.info(f"📰 Pipeline: {len(_pipeline_cache)} itens "
                 f"(bull={bull} bear={bear}) em {time.time()-start:.1f}s")
    except Exception as e:
        log.error(f"news_pipeline: {e}")


def get_news_impact(direction: str, symbol: str = "BTC") -> dict:
    """Retorna impacto consolidado das notícias para direção e símbolo."""
    if not _pipeline_cache:
        return {"impact": 0, "sentiment": "NEUTRAL", "top_news": [], "score_pts": 0}
    relevant = [
        i for i in _pipeline_cache
        if (not i.entities or
            any(e.upper() in (symbol.upper(), "BTC", "CRYPTO", "BITCOIN")
                for e in i.entities))
        and i.relevance >= 20
    ][:15]
    if not relevant:
        return {"impact": 0, "sentiment": "NEUTRAL", "top_news": [], "score_pts": 0}
    tier_w = {1: 1.0, 2: 0.7, 3: 0.4}
    w_imp  = sum(i.impact * (i.relevance / 100) * tier_w.get(i.source_tier, 0.5)
                 for i in relevant)
    w_tot  = sum((i.relevance / 100) * tier_w.get(i.source_tier, 0.5)
                 for i in relevant) or 1
    final  = w_imp / w_tot * 100
    pts    = int(max(-20, min(20, final if direction == "LONG" else -final)))
    overall = ("BULLISH" if final > 10 else "BEARISH" if final < -10 else "NEUTRAL")
    top_news = [
        {"title": i.title[:80], "source": i.source,
         "sentiment": i.sentiment, "relevance": i.relevance}
        for i in relevant[:3]
    ]
    return {"impact": round(final, 1), "sentiment": overall,
            "score_pts": pts, "top_news": top_news, "count": len(relevant)}


def get_pipeline_status() -> dict:
    """Status do pipeline para o dashboard e /api/status."""
    bull = sum(1 for i in _pipeline_cache if i.sentiment == "BULLISH")
    bear = sum(1 for i in _pipeline_cache if i.sentiment == "BEARISH")
    age  = int((time.time() - _pipeline_last_run) / 60) if _pipeline_last_run else -1
    return {
        "total": len(_pipeline_cache), "bullish": bull, "bearish": bear,
        "neutral": len(_pipeline_cache) - bull - bear,
        "age_minutes": age,
        "sources": list({i.source for i in _pipeline_cache}),
    }
