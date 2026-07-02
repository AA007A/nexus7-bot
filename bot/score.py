import aiohttp
from bot import database as db
import time
"""
BGX Capital — Pre-Trade Score System
Score 0-100 antes de qualquer entrada.
Entra apenas se score >= cfg.MIN_ENTRY_SCORE (padrão: 60).
Bot NUNCA rejeita trade por perdas anteriores.

[TÉCNICO — 40pts]
  Break of Structure / SMC confirmado  → +10
  Liquidity Sweep identificado         → +10
  Volume acima da média 20p            → +10
  RSI entre 30-70                      → +5
  Spread bid/ask < 0.05%               → +5

[ORDER FLOW — 35pts]
  CVD positivo na direção              → +10
  Open Interest crescendo              → +10
  Funding Rate favorável               → +8
  Liquidation cluster próximo          → +7

[MACRO & SENTIMENTO — 25pts]
  Fear & Greed favorável               → +10
  BTC Dominance na direção             → +8
  DXY sem força oposta                 → +7

[NOTÍCIAS — modificador, não bloqueia]
  Notícia BULLISH > 0.8                → +15 bônus
  Notícia BEARISH > 0.8               → -15 (não bloqueia)
  Evento FOMC/CPI/NFP janela 30min    → -10
"""
import asyncio, time
from typing import Optional
from bot.logger import log

# MIN_SCORE alinhado com cfg.MIN_ENTRY_SCORE para eliminar conflito de diagnóstico
# v11 tinha MIN_SCORE=50 aqui vs MIN_ENTRY_SCORE=60 no engine — logs enganosos
from bot.config import cfg as _cfg
MIN_SCORE = _cfg.MIN_ENTRY_SCORE  # sincronizado: usa sempre o valor de config.py

# Cache de dados macro (atualiza a cada 5min)
_macro_cache = {
    "fear_greed": 50,
    "btc_dominance": 57.0,
    "last_update": 0,
}

# Cache de notícias (última relevante)
_news_cache = {
    "classificacao": "NEUTRO",
    "score_confianca": 0.0,
    "impacto": 0.0,
    "timestamp": 0,
    "fomc_window": False,
}


# ── Técnico ─────────────────────────────────────────────────────
def score_tecnico(
    closes, highs, lows, volumes, direction: str,
    orderbook: dict = None,
) -> dict:
    """Calcula score técnico 0-40."""
    score = 0
    details = {}
    price = closes[-1] if closes else 0

    # 1. Break of Structure (SMC) — HH/LL confirmado
    if len(highs) >= 10 and len(lows) >= 10:
        recent_h = highs[-10:]
        recent_l = lows[-10:]
        if direction == "LONG":
            bos = recent_h[-1] > max(recent_h[:-1])
        else:
            bos = recent_l[-1] < min(recent_l[:-1])
        if bos:
            score += 10
            details["bos"] = True
        else:
            details["bos"] = False

    # 2. Liquidity Sweep — wick que rompeu e voltou
    if len(closes) >= 5 and len(highs) >= 5 and len(lows) >= 5:
        prev_high = max(highs[-6:-1])
        prev_low  = min(lows[-6:-1])
        body = abs(closes[-1] - closes[-2]) if len(closes) >= 2 else 0
        if direction == "LONG":
            sweep = lows[-1] < prev_low and closes[-1] > prev_low
        else:
            sweep = highs[-1] > prev_high and closes[-1] < prev_high
        if sweep:
            score += 10
            details["liquidity_sweep"] = True
        else:
            details["liquidity_sweep"] = False

    # 3. Volume acima da média 20p
    if len(volumes) >= 21:
        import numpy as np
        avg_vol = np.mean(volumes[-21:-1])
        vol_r   = volumes[-1] / avg_vol if avg_vol > 0 else 1
        if vol_r >= 1.5:
            score += 10
            details["volume"] = round(vol_r, 2)
        elif vol_r >= 1.0:
            score += 5
            details["volume"] = round(vol_r, 2)
        else:
            details["volume"] = round(vol_r, 2)

    # 4. RSI entre 30-70
    try:
        from bot.indicators import rsi as rsi_fn
        rsi_v = rsi_fn(closes)[-1]
        if 30 <= rsi_v <= 70:
            score += 5
        details["rsi"] = round(rsi_v, 1)
    except Exception:
        score += 3   # fallback
        details["rsi"] = 50

    # 5. Spread bid/ask rigoroso (< 0.03%)
    if orderbook:
        try:
            bids = orderbook.get("b", [[0, 0]])
            asks = orderbook.get("a", [[0, 0]])
            best_bid = float(bids[0][0]) if bids else price
            best_ask = float(asks[0][0]) if asks else price
            spread_pct = (best_ask - best_bid) / price * 100 if price > 0 else 1
            
            # Filtro de liquidez: spread alto bloqueia
            if spread_pct > 0.1:  # spread alto: sem pontos, não bloqueia
                details["liquidity_block"] = False
            elif spread_pct < 0.03:
                score += 5
            details["spread_pct"] = round(spread_pct, 4)
        except Exception:
            score += 0
    else:
        score += 3   # Sem orderbook = score neutro

    return {"score": min(40, score), "details": details}


# ── Order Flow ───────────────────────────────────────────────────
def score_orderflow(
    direction: str,
    oi_current: float = 0,
    oi_previous: float = 0,
    funding_rate: float = 0,
    cvd: float = 0,
) -> dict:
    """Calcula score de order flow 0-35."""
    score = 0
    details = {}

    # 1. CVD positivo na direção
    if direction == "LONG":
        if cvd > 0:
            score += 10
            details["cvd"] = "bullish"
        else:
            details["cvd"] = "bearish"
    else:
        if cvd < 0:
            score += 10
            details["cvd"] = "bearish"
        else:
            details["cvd"] = "bullish"

    # 2. Open Interest crescendo na direção
    if oi_previous > 0:
        oi_change = (oi_current - oi_previous) / oi_previous
        if oi_change > 0.01:   # OI crescendo > 1%
            score += 10
            details["oi_change"] = f"+{oi_change*100:.1f}%"
        elif oi_change > 0:
            score += 5
            details["oi_change"] = f"+{oi_change*100:.2f}%"
        else:
            details["oi_change"] = f"{oi_change*100:.1f}%"
    else:
        score += 5   # sem dados, neutro
        details["oi_change"] = "N/A"

    # 3. Funding Rate favorável
    # LONG favorável: funding negativo (shorts pagam longs)
    # SHORT favorável: funding positivo (longs pagam shorts)
    if direction == "LONG":
        if funding_rate < -0.0001:
            score += 8
            details["funding"] = "favorável"
        elif funding_rate < 0.0003:
            score += 4
            details["funding"] = "neutro"
        else:
            details["funding"] = "desfavorável"
    else:
        if funding_rate > 0.0001:
            score += 8
            details["funding"] = "favorável"
        elif funding_rate > -0.0003:
            score += 4
            details["funding"] = "neutro"
        else:
            details["funding"] = "desfavorável"

    # 4. Liquidation cluster (+7 — simplificado sem heatmap completo)
    # Assume neutro por padrão (sem dados externos)
    score += 4
    details["liq_cluster"] = "estimado"

    return {"score": min(35, score), "details": details}


# ── Macro & Sentimento ───────────────────────────────────────────
def score_macro(direction: str) -> dict:
    """Calcula score macro 0-25 (usa cache atualizado periodicamente)."""
    score = 0
    details = {}
    fg = _macro_cache.get("fear_greed", 50)
    btc_dom = _macro_cache.get("btc_dominance", 57.0)

    # 1. Fear & Greed Index (0-100)
    # LONG: Greed (>55) favorável; Fear (<45) pode ser reversão
    # SHORT: Fear (<45) favorável
    if direction == "LONG":
        if 45 <= fg <= 75:
            score += 10
            details["fear_greed"] = f"{fg} (neutro/favorável)"
        elif fg > 75:
            score += 5
            details["fear_greed"] = f"{fg} (greed extremo — cautela)"
        elif fg <= 25:
            score += 7   # Fear extremo = potencial reversão contrária (buy the fear)
            details["fear_greed"] = f"{fg} (fear extremo — reversão potencial)"
        else:
            score += 4
            details["fear_greed"] = f"{fg} (medo moderado)"
    else:
        if fg < 45:
            score += 10
            details["fear_greed"] = f"{fg} (favorável short)"
        else:
            score += 3
            details["fear_greed"] = f"{fg} (desfavorável short)"

    # 2. BTC Dominance
    if direction == "LONG":
        if btc_dom >= 55:
            score += 8
            details["btc_dominance"] = f"{btc_dom}% (favorável)"
        else:
            score += 4
            details["btc_dominance"] = f"{btc_dom}%"
    else:
        if btc_dom < 50:
            score += 8
            details["btc_dominance"] = f"{btc_dom}% (alt season)"
        else:
            score += 4
            details["btc_dominance"] = f"{btc_dom}%"

    # 3. DXY — via cache do market_data
    try:
        from bot.market_data import _macro_corr
        dxy = _macro_corr.get("dxy_trend", "neutral")
        if direction == "LONG":
            if dxy == "down":
                score += 7
                details["dxy"] = "down (favorável long)"
            elif dxy == "neutral":
                score += 4
                details["dxy"] = "neutro"
            else:
                score += 1
                details["dxy"] = "up (desfavorável long)"
        else:
            if dxy == "up":
                score += 7
                details["dxy"] = "up (favorável short)"
            elif dxy == "neutral":
                score += 4
                details["dxy"] = "neutro"
            else:
                score += 1
                details["dxy"] = "down (desfavorável short)"
    except Exception:
        score += 4
        details["dxy"] = "neutro (fallback)"

    return {"score": min(25, score), "details": details}


# ── Notícias — modificador ───────────────────────────────────────
def score_news_modifier(direction: str) -> dict:
    """Retorna modificador de notícias (pode ser negativo, não bloqueia)."""
    n = _news_cache
    modifier = 0
    details  = {}
    age_min  = (time.time() - n.get("timestamp", 0)) / 60

    if age_min > 60:   # notícia velha > 1h: sem impacto
        return {"modifier": 0, "details": {"news": "sem notícia recente"}}

    classif  = n.get("classificacao", "NEUTRO")
    conf     = n.get("score_confianca", 0.0)
    is_fomc  = n.get("fomc_window", False)

    if is_fomc:
        modifier -= 10  # Reduz mas não bloqueia
        details["fomc"] = "janela FOMC/CPI/NFP -10pts"

    if conf >= 0.8:
        if classif == "BULLISH" and direction == "LONG":
            modifier += 15
            details["news"] = f"BULLISH +15 (conf={conf:.2f})"
        elif classif == "BEARISH" and direction == "SHORT":
            modifier += 15
            details["news"] = f"BEARISH SHORT +15 (conf={conf:.2f})"
        elif classif == "BULLISH" and direction == "SHORT":
            modifier -= 15
            details["news"] = f"BULLISH contra SHORT -15"
        elif classif == "BEARISH" and direction == "LONG":
            modifier -= 15
            details["news"] = f"BEARISH contra LONG -15"

    return {"modifier": modifier, "details": details}


# ── Score total ─────────────────────────────────────────────────
async def calculate(
    symbol: str,
    direction: str,
    closes, highs, lows, volumes,
    client=None,
) -> dict:
    """
    Calcula score completo 0-100+.
    Fault-tolerant: falhas parciais usam scores neutros.
    """
    orderbook   = None
    oi_current  = 0
    oi_previous = 0
    funding     = 0.0
    cvd         = 0.0

    if client:
        try:
            orderbook = await client.get_orderbook(symbol)
        except Exception:
            pass
        try:
            oi_data    = await client.get_open_interest(symbol)
            oi_current = float(oi_data.get("openInterest", 0))
        except Exception:
            pass
        try:
            funding = await client.get_funding_rate(symbol)
        except Exception:
            pass

    # Calcula CVD simples (soma de body * volume com sinal)
    if len(closes) >= 2:
        cvd = sum(
            (closes[i] - closes[i-1]) / abs(closes[i-1]) * volumes[i]
            if closes[i-1] > 0 else 0
            for i in range(max(0, len(closes)-20), len(closes))
        )

    s_tec = score_tecnico(closes, highs, lows, volumes, direction, orderbook)
    s_of  = score_orderflow(direction, oi_current, oi_previous, funding, cvd)
    s_mac = score_macro(direction)
    s_news= score_news_modifier(direction)

    base  = s_tec["score"] + s_of["score"] + s_mac["score"]
    total = base + s_news["modifier"]
    total = max(0, total)   # não vai negativo

    result = {
        "total":       total,
        "tecnico":     s_tec["score"],
        "orderflow":   s_of["score"],
        "macro":       s_mac["score"],
        "news_mod":    s_news["modifier"],
        "aprovado":    total >= MIN_SCORE,
        "detalhes": {
            "tecnico":   s_tec["details"],
            "orderflow": s_of["details"],
            "macro":     s_mac["details"],
            "news":      s_news["details"],
        },
        "oi":      oi_current,
        "funding": funding,
        "cvd":     round(cvd, 2),
    }

    log.info(
        f"[{symbol}] Score={total}/100 {direction} "
        f"(TEC={s_tec['score']} OF={s_of['score']} MAC={s_mac['score']} NEWS={s_news['modifier']:+d}) "
        f"{'✅ APROVADO' if result['aprovado'] else '❌ REPROVADO'}"
    )

    # Persiste no banco
    try:
        await db.save_signal(
            symbol, direction,
            {"total": total, "tecnico": s_tec["score"],
             "orderflow": s_of["score"], "macro": s_mac["score"],
             "news": s_news["modifier"]},
            entrou=result["aprovado"],
            motivo="" if result["aprovado"] else f"score {total} < {MIN_SCORE}",
        )
    except Exception:
        pass

    return result


# ── Atualiza macro cache ─────────────────────────────────────────
async def update_macro_cache():
    """Atualiza Fear & Greed + BTC Dominance a cada 5 minutos."""
    while True:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get("https://api.alternative.me/fng/?limit=1",
                                  timeout=aiohttp.ClientTimeout(total=8)) as r:
                    data = await r.json()
                    _macro_cache["fear_greed"] = int(
                        data["data"][0]["value"]
                    )
        except Exception:
            pass
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get("https://api.coingecko.com/api/v3/global",
                                  timeout=aiohttp.ClientTimeout(total=8)) as r:
                    data = await r.json()
                    btc_dom = data.get("data", {}).get(
                        "market_cap_percentage", {}
                    ).get("btc", 57.0)
                    _macro_cache["btc_dominance"] = round(btc_dom, 1)
        except Exception:
            pass
        _macro_cache["last_update"] = time.time()
        await asyncio.sleep(300)   # atualiza a cada 5 min


# ── News reader ─────────────────────────────────────────────────
_MACRO_KEYWORDS = ["fomc", "cpi", "nfp", "interest rate", "fed ", "federal reserve",
                   "inflation", "jobs report", "gdp"]
_BULLISH_KW     = ["bull", "surge", "rally", "breakout", "ath", "adoption",
                   "etf", "approval", "buy", "long", "support"]
_BEARISH_KW     = ["bear", "crash", "dump", "ban", "hack", "sell", "short",
                   "regulation", "sec", "lawsuit", "collapse"]


def _classify_news(text: str) -> tuple:
    """Retorna (classificacao, confianca, is_fomc)."""
    tl = text.lower()
    is_fomc   = any(kw in tl for kw in _MACRO_KEYWORDS)
    bull_hits = sum(1 for kw in _BULLISH_KW if kw in tl)
    bear_hits = sum(1 for kw in _BEARISH_KW if kw in tl)

    if bull_hits > bear_hits:
        conf  = min(0.95, 0.5 + bull_hits * 0.1)
        classif = "BULLISH"
    elif bear_hits > bull_hits:
        conf  = min(0.95, 0.5 + bear_hits * 0.1)
        classif = "BEARISH"
    else:
        conf  = 0.3
        classif = "NEUTRO"

    return classif, conf, is_fomc


async def news_reader_loop():
    """Monitora CryptoPanic + RSS 24/7. Fault-tolerant."""
    CRYPTOPANIC = "https://cryptopanic.com/api/v1/posts/?auth_token=&filter=important&currencies=BTC"
    RSS_FEEDS   = [
        "https://cointelegraph.com/rss",
        "https://theblock.co/rss.xml",
    ]
    while True:
        try:
            async with aiohttp.ClientSession() as s:
                # CryptoPanic
                try:
                    async with s.get(CRYPTOPANIC, timeout=aiohttp.ClientTimeout(total=8)) as r:
                        data = await r.json()
                        for item in (data.get("results") or [])[:5]:
                            title = item.get("title", "")
                            if not title:
                                continue
                            classif, conf, is_fomc = _classify_news(title)
                            impacto = 15 if conf >= 0.8 else 5
                            _news_cache.update({
                                "classificacao":  classif,
                                "score_confianca":conf,
                                "impacto":        impacto,
                                "timestamp":      time.time(),
                                "fomc_window":    is_fomc,
                            })
                            try:
                                await db.save_news(title, "CryptoPanic",
                                                   classif, conf, impacto)
                            except Exception:
                                pass
                            log.info(f"📰 News: {classif} conf={conf:.2f} — {title[:60]}")
                            break
                except Exception:
                    pass

                # RSS feeds
                try:
                    import feedparser
                    for feed_url in RSS_FEEDS:
                        try:
                            async with s.get(feed_url, timeout=aiohttp.ClientTimeout(total=6)) as r:
                                content = await r.text()
                            feed = feedparser.parse(content)
                            for entry in feed.entries[:3]:
                                title = entry.get("title", "")
                                classif, conf, is_fomc = _classify_news(title)
                                if conf >= 0.7:
                                    _news_cache.update({
                                        "classificacao":  classif,
                                        "score_confianca":conf,
                                        "timestamp":      time.time(),
                                        "fomc_window":    is_fomc,
                                    })
                                    break
                        except Exception:
                            pass
                except Exception:
                    pass
        except Exception as e:
            log.debug(f"news_reader: {e}")

        await asyncio.sleep(120)   # a cada 2 minutos
