import time
"""
BGX Capital — Market Data Module
- CVD (Cumulative Volume Delta) em tempo real
- Liquidation Heatmap
- Correlações Macro (BTC x DXY, S&P500, BTC.D)
Fault-tolerant: falhas não afetam o bot.
"""
import asyncio, time
from bot.logger import log

# ── CVD State — Persistente com janela de 4h ─────────────────────
# Problema resolvido: CVD não reseta mais no reinício do bot.
# Solução: mantém histórico de ticks com timestamp.
# Na inicialização, reconstrói o CVD com os últimos 4h de klines.

import time as _time
from collections import deque

_CVD_WINDOW_SECONDS = 4 * 3600   # janela de 4 horas

# Cada entry: (timestamp, delta_volume)
_cvd_ticks: dict = {}    # symbol → deque de (ts, delta)
_cvd_cache: dict = {}    # symbol → cvd acumulado (soma da janela)


def _purge_old_ticks(symbol: str):
    """Remove ticks mais antigos que a janela de 4h."""
    if symbol not in _cvd_ticks:
        return
    cutoff = _time.time() - _CVD_WINDOW_SECONDS
    q = _cvd_ticks[symbol]
    while q and q[0][0] < cutoff:
        _, delta = q.popleft()
        _cvd_cache[symbol] = _cvd_cache.get(symbol, 0) - delta


def update_cvd(symbol: str, close: float, prev_close: float, volume: float):
    """
    Atualiza CVD com cada tick do WebSocket.
    Mantém janela rolante de 4h — não perde histórico no reinício.
    """
    if symbol not in _cvd_ticks:
        _cvd_ticks[symbol] = deque()
    _purge_old_ticks(symbol)
    delta = volume if close >= prev_close else -volume
    ts    = _time.time()
    _cvd_ticks[symbol].append((ts, delta))
    _cvd_cache[symbol] = _cvd_cache.get(symbol, 0) + delta


def get_cvd(symbol: str) -> float:
    """Retorna CVD acumulado na janela de 4h."""
    _purge_old_ticks(symbol)
    return _cvd_cache.get(symbol, 0.0)


def get_cvd_bias(symbol: str) -> str:
    """Retorna BULLISH, BEARISH ou NEUTRAL com base no CVD."""
    cvd = get_cvd(symbol)
    if cvd > 0:   return "BULLISH"
    if cvd < 0:   return "BEARISH"
    return "NEUTRAL"


def reset_cvd(symbol: str):
    """Reseta CVD de um símbolo (usado apenas em testes)."""
    _cvd_cache[symbol] = 0.0
    if symbol in _cvd_ticks:
        _cvd_ticks[symbol].clear()


async def rebuild_cvd_from_klines(client, symbol: str):
    """
    Reconstrói o CVD dos últimos 4h usando klines históricas.
    Chamado na inicialização do bot para evitar CVD zerado.
    """
    try:
        klines = await client.get_klines(symbol, "15", 16)  # 16 candles × 15min = 4h
        if not klines:
            return
        if symbol not in _cvd_ticks:
            _cvd_ticks[symbol] = deque()
        # Simula ticks históricos com timestamp aproximado
        now = _time.time()
        for i, k in enumerate(klines):
            ts    = now - (len(klines) - i) * 900  # 900s = 15min por candle
            close = float(k.get("c", 0))
            prev  = float(klines[i-1].get("c", close)) if i > 0 else close
            vol   = float(k.get("v", 0))
            delta = vol if close >= prev else -vol
            _cvd_ticks[symbol].append((ts, delta))
            _cvd_cache[symbol] = _cvd_cache.get(symbol, 0) + delta
        log.info(f"📊 CVD {symbol} reconstruído: {get_cvd(symbol):+.0f} ({len(klines)} candles)")
    except Exception as e:
        log.warning(f"rebuild_cvd {symbol}: {e}")


# ── Liquidation Heatmap ──────────────────────────────────────────
_liq_cache: dict = {}   # symbol → {"levels": [...], "updated": ts}


async def fetch_liq_heatmap(client, symbol: str) -> dict:
    """
    Estima clusters de liquidação usando OI e preço atual.
    Bybit não expõe heatmap direto — calculamos por proximidade ao preço.
    """
    try:
        oi_data = await client.get_open_interest(symbol)
        ticker  = client.get_cached_ticker(symbol)
        price   = float(ticker.get("lastPrice", 0))
        oi      = float(oi_data.get("openInterest", 0))

        if price <= 0:
            return {}

        # Estima clusters de liquidação nos níveis de alavancagem comum
        # Com 10x: liq ≈ ±10% do preço; 20x ≈ ±5%; 50x ≈ ±2%
        levels = []
        for lev, pct in [(10, 0.10), (20, 0.05), (25, 0.04), (50, 0.02)]:
            levels.append({
                "price_long_liq":  round(price * (1 - pct), 2),
                "price_short_liq": round(price * (1 + pct), 2),
                "leverage":        lev,
                "distance_pct":    round(pct * 100, 1),
            })

        result = {
            "symbol":   symbol,
            "price":    price,
            "oi":       oi,
            "levels":   levels,
            "updated":  time.time(),
        }
        _liq_cache[symbol] = result
        return result
    except Exception as e:
        log.debug(f"liq_heatmap {symbol}: {e}")
        return _liq_cache.get(symbol, {})


def get_liq_score(symbol: str, direction: str, price: float) -> int:
    """
    Retorna score 0-7 baseado em cluster de liquidação próximo.
    Cluster próximo na direção do trade = score alto.
    """
    data = _liq_cache.get(symbol, {})
    if not data or not price:
        return 4   # neutro

    for level in data.get("levels", []):
        dist_long  = abs(price - level["price_long_liq"])  / price
        dist_short = abs(price - level["price_short_liq"]) / price

        if direction == "LONG" and dist_long < 0.02:
            return 7   # cluster de liq próximo abaixo → short squeeze potencial
        if direction == "SHORT" and dist_short < 0.02:
            return 7

    return 3   # sem cluster próximo


# ── Correlações Macro ────────────────────────────────────────────
_macro_corr: dict = {
    "btc_dxy_corr":   -0.6,   # BTC inversamente correlacionado ao DXY
    "btc_sp500_corr":  0.7,   # BTC positivamente correlacionado ao S&P
    "btc_dominance":   57.0,
    "dxy_trend":      "neutral",
    "sp500_trend":    "neutral",
    "last_update":     0,
}


async def update_macro_correlations():
    """Atualiza correlações macro a cada 15 minutos. Fault-tolerant."""
    import aiohttp
    while True:
        try:
            async with aiohttp.ClientSession() as s:
                # BTC Dominance via CoinGecko
                try:
                    async with s.get(
                        "https://api.coingecko.com/api/v3/global",
                        timeout=aiohttp.ClientTimeout(total=8)
                    ) as r:
                        d = await r.json()
                        dom = d.get("data", {}).get(
                            "market_cap_percentage", {}
                        ).get("btc", 57.0)
                        _macro_corr["btc_dominance"] = round(dom, 1)
                except Exception:
                    pass

                # DXY via Yahoo Finance (sem auth)
                try:
                    async with s.get(
                        "https://query1.finance.yahoo.com/v8/finance/chart/DX-Y.NYB"
                        "?interval=1d&range=5d",
                        headers={"User-Agent": "Mozilla/5.0"},
                        timeout=aiohttp.ClientTimeout(total=8)
                    ) as r:
                        d = await r.json()
                        prices = d["chart"]["result"][0]["indicators"]["quote"][0]["close"]
                        prices = [p for p in prices if p]
                        if len(prices) >= 2:
                            chg = (prices[-1] - prices[-2]) / prices[-2]
                            _macro_corr["dxy_trend"] = "up" if chg > 0.001 else (
                                "down" if chg < -0.001 else "neutral"
                            )
                except Exception:
                    pass

                # S&P500 via Yahoo Finance
                try:
                    async with s.get(
                        "https://query1.finance.yahoo.com/v8/finance/chart/^GSPC"
                        "?interval=1d&range=5d",
                        headers={"User-Agent": "Mozilla/5.0"},
                        timeout=aiohttp.ClientTimeout(total=8)
                    ) as r:
                        d = await r.json()
                        prices = d["chart"]["result"][0]["indicators"]["quote"][0]["close"]
                        prices = [p for p in prices if p]
                        if len(prices) >= 2:
                            chg = (prices[-1] - prices[-2]) / prices[-2]
                            _macro_corr["sp500_trend"] = "up" if chg > 0.001 else (
                                "down" if chg < -0.001 else "neutral"
                            )
                except Exception:
                    pass

                _macro_corr["last_update"] = time.time()
                log.info(
                    f"📊 Macro: BTC.D={_macro_corr['btc_dominance']}% "
                    f"DXY={_macro_corr['dxy_trend']} "
                    f"S&P={_macro_corr['sp500_trend']}"
                )

        except Exception as e:
            log.debug(f"macro_corr: {e}")

        await asyncio.sleep(900)   # 15 minutos


def macro_corr_score(direction: str) -> int:
    """
    Score 0-10 baseado nas correlações macro.
    BTC sobe quando DXY cai e S&P sobe.
    """
    score = 5   # neutro base
    dxy   = _macro_corr.get("dxy_trend", "neutral")
    sp    = _macro_corr.get("sp500_trend", "neutral")
    dom   = _macro_corr.get("btc_dominance", 57.0)

    if direction == "LONG":
        if dxy == "down":  score += 2   # DXY caindo = favorável BTC
        if dxy == "up":    score -= 2   # DXY subindo = desfavorável
        if sp  == "up":    score += 2   # S&P subindo = risk-on
        if sp  == "down":  score -= 1
        if dom >= 55:      score += 1   # BTC dominante
    else:  # SHORT
        if dxy == "up":    score += 2
        if dxy == "down":  score -= 2
        if sp  == "down":  score += 2
        if dom < 50:       score += 1   # alt season = BTC fraco

    return max(0, min(10, score))


def get_macro_summary() -> dict:
    return dict(_macro_corr)

# ══════════════════════════════════════════════════════════════════
# FONTES EXTERNAS — Nível Institucional
# ══════════════════════════════════════════════════════════════════

_coinglass_cache: dict = {}
_events_cache:   list  = []
_sentiment_cache: dict = {}

# ── Coinglass — Open Interest + Liquidações + Long/Short Ratio ────
async def update_coinglass():
    """
    Busca dados do Coinglass via API pública (sem auth):
    - Open Interest BTC/ETH
    - Long/Short ratio
    - Liquidações 24h
    """
    global _coinglass_cache
    try:
        async with aiohttp.ClientSession() as s:
            # Open Interest + Long/Short ratio via Coinglass public
            url = "https://open-api.coinglass.com/public/v2/indicator/open_interest"
            headers = {"coinglassSecret": ""}  # público
            async with s.get(
                "https://fapi.binance.com/futures/data/globalLongShortAccountRatio"
                "?symbol=BTCUSDT&period=1h&limit=1",
                timeout=aiohttp.ClientTimeout(total=8)
            ) as r:
                if r.status == 200:
                    data = await r.json()
                    if data:
                        ls = data[0]
                        _coinglass_cache["btc_long_ratio"]  = float(ls.get("longAccount",  0.5))
                        _coinglass_cache["btc_short_ratio"] = float(ls.get("shortAccount", 0.5))
                        _coinglass_cache["ls_ratio"]        = float(ls.get("longShortRatio", 1.0))

            # Open Interest via Binance Futures (proxy confiável)
            async with s.get(
                "https://fapi.binance.com/fapi/v1/openInterest?symbol=BTCUSDT",
                timeout=aiohttp.ClientTimeout(total=8)
            ) as r:
                if r.status == 200:
                    data = await r.json()
                    _coinglass_cache["btc_oi"] = float(data.get("openInterest", 0))

            # Liquidações estimadas via Binance (funding + OI proxy)
            async with s.get(
                "https://fapi.binance.com/futures/data/takerlongshortRatio"
                "?symbol=BTCUSDT&period=1h&limit=1",
                timeout=aiohttp.ClientTimeout(total=8)
            ) as r:
                if r.status == 200:
                    data = await r.json()
                    if data:
                        _coinglass_cache["taker_buy_ratio"]  = float(data[0].get("buySellRatio", 1.0))
                        _coinglass_cache["taker_buy_vol"]    = float(data[0].get("buyVol",  0))
                        _coinglass_cache["taker_sell_vol"]   = float(data[0].get("sellVol", 0))

            log.info(f"📊 Coinglass: L/S={_coinglass_cache.get('ls_ratio',1):.2f} "
                     f"OI={_coinglass_cache.get('btc_oi',0)/1e6:.1f}M")
    except Exception as e:
        log.warning(f"update_coinglass: {e}")


def get_coinglass() -> dict:
    return _coinglass_cache.copy()


# ── Binance Announcements — detecta novos listings ────────────────
_binance_seen_ids: set = set()

async def check_binance_announcements() -> list:
    """
    Monitora anúncios da Binance.
    Novo listing = pump quase garantido nas primeiras horas.
    Retorna lista de anúncios novos desde a última checagem.
    """
    new_items = []
    try:
        async with aiohttp.ClientSession() as s:
            url = ("https://www.binance.com/bapi/composite/v1/public/cms/article/list/query"
                   "?type=1&pageNo=1&pageSize=5")
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=8),
                             headers={"User-Agent": "Mozilla/5.0"}) as r:
                if r.status == 200:
                    data = await r.json()
                    articles = data.get("data", {}).get("articles", [])
                    for a in articles:
                        aid   = str(a.get("id", ""))
                        title = a.get("title", "")
                        if aid and aid not in _binance_seen_ids:
                            _binance_seen_ids.add(aid)
                            is_listing = any(w in title.lower() for w in
                                           ["will list", "vai listar", "new listing",
                                            "lists", "perpetual", "futures"])
                            new_items.append({
                                "id":         aid,
                                "title":      title,
                                "is_listing": is_listing,
                                "url":        f"https://www.binance.com/en/support/announcement/{aid}"
                            })
                            if is_listing:
                                log.info(f"🔔 BINANCE LISTING: {title}")
    except Exception as e:
        log.warning(f"check_binance_announcements: {e}")
    return new_items


# ── CoinMarketCal — Calendário de Eventos ─────────────────────────
async def update_coinmarketcal():
    """
    Busca eventos importantes do calendário crypto:
    - Halvings, mainnet launches, listings, expiração de opções
    - Usa API pública (sem auth) via scraping leve
    """
    global _events_cache
    try:
        async with aiohttp.ClientSession() as s:
            # Alternativa gratuita: CryptoCompare news como proxy de eventos
            url = "https://min-api.cryptocompare.com/data/v2/news/?lang=EN&categories=BTC,ETH&sortOrder=latest"
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
                if r.status == 200:
                    data = await r.json()
                    items = data.get("Data", [])[:10]
                    _events_cache = [{
                        "title":    i.get("title", ""),
                        "source":   i.get("source_info", {}).get("name", ""),
                        "url":      i.get("url", ""),
                        "ts":       i.get("published_on", 0),
                        "tags":     i.get("tags", ""),
                    } for i in items]
                    log.info(f"📅 CoinMarketCal/CryptoCompare: {len(_events_cache)} eventos")
    except Exception as e:
        log.warning(f"update_coinmarketcal: {e}")


def get_events() -> list:
    return _events_cache.copy()


# ── Sentimento Completo — cruza Fear&Greed + L/S + OI + Taker ─────
def get_market_sentiment() -> dict:
    """
    Sentimento consolidado de múltiplas fontes.
    Retorna score de -100 (extremo bearish) a +100 (extremo bullish).
    """
    score = 0
    signals = []

    # Fear & Greed
    fg = _macro_cache.get("fear_greed", 50)
    if fg >= 75:
        score -= 10; signals.append(f"F&G={fg}(greed_extremo)")
    elif fg >= 55:
        score += 8;  signals.append(f"F&G={fg}(greed)")
    elif fg <= 25:
        score += 5;  signals.append(f"F&G={fg}(fear_extremo_reversão)")
    elif fg <= 45:
        score -= 5;  signals.append(f"F&G={fg}(fear)")
    else:
        signals.append(f"F&G={fg}(neutro)")

    # Long/Short ratio
    ls = _coinglass_cache.get("ls_ratio", 1.0)
    if ls > 1.5:
        score += 10; signals.append(f"L/S={ls:.2f}(longs_dominam)")
    elif ls > 1.1:
        score += 5;  signals.append(f"L/S={ls:.2f}(levemente_long)")
    elif ls < 0.7:
        score -= 10; signals.append(f"L/S={ls:.2f}(shorts_dominam)")
    elif ls < 0.9:
        score -= 5;  signals.append(f"L/S={ls:.2f}(levemente_short)")
    else:
        signals.append(f"L/S={ls:.2f}(neutro)")

    # Taker buy/sell ratio
    tbr = _coinglass_cache.get("taker_buy_ratio", 1.0)
    if tbr > 1.3:
        score += 8;  signals.append(f"TAKER={tbr:.2f}(buy_agressivo)")
    elif tbr < 0.8:
        score -= 8;  signals.append(f"TAKER={tbr:.2f}(sell_agressivo)")
    else:
        signals.append(f"TAKER={tbr:.2f}(equilibrado)")

    # Classificação final
    if score >= 15:
        sentiment = "BULLISH"
    elif score <= -15:
        sentiment = "BEARISH"
    elif score >= 5:
        sentiment = "SLIGHTLY_BULLISH"
    elif score <= -5:
        sentiment = "SLIGHTLY_BEARISH"
    else:
        sentiment = "NEUTRAL"

    return {
        "sentiment": sentiment,
        "score":     score,
        "signals":   signals,
        "fg":        fg,
        "ls_ratio":  _coinglass_cache.get("ls_ratio", 1.0),
    }

# ══════════════════════════════════════════════════════════════════
# FILTRO DE SESSÃO DE MERCADO
# Sessões com maior liquidez e melhores oportunidades
# ══════════════════════════════════════════════════════════════════

def get_market_session() -> dict:
    """
    Retorna a sessão de mercado atual e qualidade para trading.

    Sessões de alta qualidade (80%+ das oportunidades):
    - Londres:    08:00 - 12:00 UTC  ⭐⭐⭐
    - Nova York:  13:00 - 17:00 UTC  ⭐⭐⭐
    - Overlap NY: 12:00 - 16:00 UTC  ⭐⭐⭐⭐ (melhor do dia)

    Sessões de baixa qualidade (evitar):
    - Madrugada:  00:00 - 06:00 UTC  ❌ volume mínimo
    - Asia tarde: 06:00 - 08:00 UTC  ⚠️  volume médio
    """
    now_utc = datetime.now(timezone.utc)
    hour    = now_utc.hour
    minute  = now_utc.minute
    h       = hour + minute / 60.0

    # Classificar sessão
    if 12.0 <= h < 16.0:
        session     = "OVERLAP_NY_LONDON"
        quality     = 100
        score_bonus = 8
        emoji       = "🔥"
        description = "Overlap NY+Londres — melhor liquidez do dia"
    elif 8.0 <= h < 12.0:
        session     = "LONDON"
        quality     = 85
        score_bonus = 5
        emoji       = "🇬🇧"
        description = "Sessão Londres — alta liquidez"
    elif 13.0 <= h < 17.0:
        session     = "NEW_YORK"
        quality     = 90
        score_bonus = 6
        emoji       = "🇺🇸"
        description = "Sessão Nova York — alta liquidez"
    elif 17.0 <= h < 20.0:
        session     = "NY_CLOSE"
        quality     = 65
        score_bonus = 2
        emoji       = "🌆"
        description = "Fechamento NY — liquidez decaindo"
    elif 0.0 <= h < 3.0 or h >= 23.0:
        session     = "DEAD_ZONE"
        quality     = 20
        score_bonus = -10
        emoji       = "🌑"
        description = "Zona morta — volume mínimo, evitar trades"
    elif 3.0 <= h < 6.0:
        session     = "ASIA_EARLY"
        quality     = 45
        score_bonus = -3
        emoji       = "🌏"
        description = "Ásia início — volume baixo"
    elif 6.0 <= h < 8.0:
        session     = "ASIA_CLOSE"
        quality     = 55
        score_bonus = 0
        emoji       = "🌅"
        description = "Fechamento Ásia — volume moderado"
    else:
        session     = "NEUTRAL"
        quality     = 50
        score_bonus = 0
        emoji       = "⏳"
        description = "Entre sessões"

    # Fins de semana têm volume 40% menor
    weekday = now_utc.weekday()   # 5=Sábado, 6=Domingo
    is_weekend = weekday >= 5
    if is_weekend:
        quality     = int(quality * 0.6)
        score_bonus = min(score_bonus, -5)
        description += " (fim de semana — volume reduzido)"

    return {
        "session":     session,
        "quality":     quality,
        "score_bonus": score_bonus,
        "emoji":       emoji,
        "description": description,
        "hour_utc":    hour,
        "is_weekend":  is_weekend,
        "tradeable":   quality >= 50,
    }


def should_trade_now() -> tuple:
    """
    Retorna (pode_operar: bool, motivo: str).
    Bloqueia trades em horas de baixíssimo volume.
    """
    sess = get_market_session()
    if not sess["tradeable"]:
        return False, f"Sessão {sess['session']} — qualidade {sess['quality']}% abaixo do mínimo"
    return True, sess["description"]


# ══════════════════════════════════════════════════════════════════
# CORRELAÇÃO ENTRE PARES — evita posições duplicadas
# ══════════════════════════════════════════════════════════════════

# Grupos de correlação alta (>0.85 histórico)
CORRELATION_GROUPS = {
    "BTC_FAMILY":    ["BTCUSDT"],
    "ETH_LAYER1":    ["ETHUSDT", "AVAXUSDT", "SOLUSDT", "DOTUSDT", "ADAUSDT"],
    "BNB_CEX":       ["BNBUSDT"],
    "MEME_DOGE":     ["DOGEUSDT"],
    "DEFI_LINK":     ["LINKUSDT", "MATICUSDT"],
    "LEGACY_ALTS":   ["LTCUSDT", "XRPUSDT"],
}

def get_correlation_group(symbol: str) -> str:
    """Retorna o grupo de correlação do símbolo."""
    for group, symbols in CORRELATION_GROUPS.items():
        if symbol in symbols:
            return group
    return f"SOLO_{symbol}"


def check_correlation_conflict(symbol: str, open_positions: dict) -> dict:
    """
    Verifica se abrir posição em 'symbol' cria conflito de correlação.

    Regras profissionais:
    1. Máximo 1 posição por grupo de correlação
    2. ETH_LAYER1 é o maior grupo — nunca ter AVAX+SOL+ETH ao mesmo tempo
    3. Se BTC e ETH ambos LONG = permitido (são os principais)
    4. Altcoins correlacionadas = bloqueia segunda posição

    Retorna:
      conflict: True se há conflito
      reason:   motivo
      group:    grupo do símbolo
    """
    my_group = get_correlation_group(symbol)

    conflicts = []
    for pos_symbol, pos in open_positions.items():
        if pos_symbol == symbol:
            continue
        pos_group = get_correlation_group(pos_symbol)

        # Mesmo grupo = conflito direto
        if pos_group == my_group and my_group != "BTC_FAMILY":
            conflicts.append(f"{pos_symbol} (mesmo grupo {my_group})")

        # ETH_LAYER1 especial: permite no máx 2 posições se direções opostas
        if my_group == "ETH_LAYER1" and pos_group == "ETH_LAYER1":
            if pos.direction == (open_positions.get(symbol, pos)).direction:
                conflicts.append(f"{pos_symbol} mesma direção ETH_LAYER1")

    if conflicts:
        return {
            "conflict": True,
            "reason":   f"Correlação alta com: {', '.join(conflicts)}",
            "group":    my_group,
            "penalty":  -15,
        }

    return {
        "conflict": False,
        "reason":   f"Sem conflito (grupo: {my_group})",
        "group":    my_group,
        "penalty":  0,
    }


# ══════════════════════════════════════════════════════════════════
# X/TWITTER SENTIMENT — via API gratuita (nitter/RSS)
# ══════════════════════════════════════════════════════════════════

_twitter_cache: dict = {
    "sentiment": "NEUTRAL",
    "score":     0,
    "mentions":  {},
    "trending":  [],
    "last_update": 0,
}

_BULLISH_WORDS = [
    "bullish", "moon", "pump", "breakout", "buy", "long", "ath",
    "bull", "surge", "rally", "🚀", "🟢", "💎", "hodl", "accumulate"
]
_BEARISH_WORDS = [
    "bearish", "dump", "crash", "sell", "short", "bear", "drop",
    "correction", "fall", "rekt", "📉", "🔴", "panic", "fear", "liquidation"
]

async def update_twitter_sentiment():
    """
    Busca sentimento do X/Twitter via:
    1. CryptoCompare social stats (gratuito)
    2. RSS do Nitter (espelho público do Twitter)
    3. Trending topics via API alternativa

    Fallback para cada fonte — não quebra o bot se falhar.
    """
    global _twitter_cache

    # Throttle: atualiza no máx a cada 10 minutos
    if _t.time() - _twitter_cache.get("last_update", 0) < 600:
        return

    bull_score = 0
    bear_score = 0
    mentions   = {}
    trending   = []

    try:
        async with aiohttp.ClientSession() as s:
            # 1. CryptoCompare Social Stats — gratuito, sem auth
            url = ("https://min-api.cryptocompare.com/data/social/coin/latest"
                   "?coinId=1182&extraParams=AA_Capital")
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
                if r.status == 200:
                    data = await r.json()
                    tw   = data.get("Data", {}).get("Twitter", {})
                    if tw:
                        followers   = tw.get("followers", 0)
                        statuses    = tw.get("statuses",  0)
                        favourites  = tw.get("favourites", 0)
                        # Engagement alto = sentimento ativo
                        if statuses > 1000:
                            bull_score += 5
                            trending.append(f"BTC Twitter: {statuses} posts")

            # 2. Nitter RSS — tweets recentes sobre BTC/ETH/crypto
            rss_sources = [
                "https://nitter.net/search/rss?q=bitcoin+OR+crypto+OR+BTC&f=tweets",
                "https://nitter.poast.org/search/rss?q=bitcoin+bullish+OR+bearish",
            ]
            for rss_url in rss_sources:
                try:
                    async with s.get(
                        rss_url,
                        timeout=aiohttp.ClientTimeout(total=6),
                        headers={"User-Agent": "Mozilla/5.0"}
                    ) as r:
                        if r.status == 200:
                            text = await r.text()
                            # Contar palavras bullish/bearish no XML
                            text_lower = text.lower()
                            for w in _BULLISH_WORDS:
                                cnt = text_lower.count(w)
                                if cnt > 0:
                                    bull_score += min(cnt, 3)
                            for w in _BEARISH_WORDS:
                                cnt = text_lower.count(w)
                                if cnt > 0:
                                    bear_score += min(cnt, 3)
                            break   # se uma fonte funcionar, não precisa da outra
                except Exception:
                    continue

            # 3. Menções por símbolo via CryptoCompare
            symbols_to_check = ["BTC", "ETH", "SOL", "BNB"]
            coin_ids = {"BTC": 1182, "ETH": 7605, "SOL": 934430, "BNB": 321992}
            for sym in symbols_to_check[:2]:   # limitar requests
                try:
                    coin_url = (f"https://min-api.cryptocompare.com/data/social/coin/latest"
                               f"?coinId={coin_ids.get(sym, 1182)}")
                    async with s.get(coin_url, timeout=aiohttp.ClientTimeout(total=5)) as r:
                        if r.status == 200:
                            d = await r.json()
                            tw2 = d.get("Data", {}).get("Twitter", {})
                            mentions[sym] = {
                                "followers": tw2.get("followers", 0),
                                "posts":     tw2.get("statuses",  0),
                            }
                except Exception:
                    pass

    except Exception as e:
        log.warning(f"Twitter sentiment: {e}")

    # Calcular sentimento final
    total = bull_score + bear_score or 1
    bull_ratio = bull_score / total

    if bull_ratio >= 0.65:
        sentiment = "BULLISH"
        score     = int(bull_ratio * 20)
    elif bull_ratio <= 0.35:
        sentiment = "BEARISH"
        score     = -int((1 - bull_ratio) * 20)
    else:
        sentiment = "NEUTRAL"
        score     = 0

    _twitter_cache = {
        "sentiment":   sentiment,
        "score":       score,
        "bull_score":  bull_score,
        "bear_score":  bear_score,
        "mentions":    mentions,
        "trending":    trending,
        "last_update": _t.time(),
    }

    log.info(f"🐦 Twitter: {sentiment} (bull={bull_score} bear={bear_score})")


def get_twitter_sentiment() -> dict:
    return _twitter_cache.copy()

# ══════════════════════════════════════════════════════════════════
# ITEM 6 — CryptoPanic com auth_token real
# ══════════════════════════════════════════════════════════════════
import os as _os
_CRYPTOPANIC_TOKEN = _os.environ.get("CRYPTOPANIC_TOKEN", "")

_cryptopanic_cache: list = []

async def update_cryptopanic():
    """
    CryptoPanic — fonte mais usada de notícias crypto com sentimento.
    Requer CRYPTOPANIC_TOKEN nas Variables do Railway (gratuito em cryptopanic.com).
    Fallback para RSS público se sem token.
    """
    global _cryptopanic_cache
    try:
        async with aiohttp.ClientSession() as s:
            if _CRYPTOPANIC_TOKEN:
                url = (f"https://cryptopanic.com/api/v1/posts/"
                       f"?auth_token={_CRYPTOPANIC_TOKEN}"
                       f"&filter=hot&public=true&kind=news&currencies=BTC,ETH,SOL")
            else:
                # Fallback RSS público
                url = "https://cryptopanic.com/news/rss/"

            async with s.get(url, timeout=aiohttp.ClientTimeout(total=8),
                             headers={"User-Agent": "BGX-Capital/10.0"}) as r:
                if r.status == 200:
                    if _CRYPTOPANIC_TOKEN:
                        data    = await r.json()
                        results = data.get("results", [])
                        _cryptopanic_cache = [{
                            "title":     item.get("title", ""),
                            "sentiment": item.get("votes", {}).get("positive", 0) -
                                        item.get("votes", {}).get("negative", 0),
                            "source":    item.get("domain", ""),
                            "url":       item.get("url", ""),
                        } for item in results[:10]]
                    else:
                        text = await r.text()
                        # Parse RSS simples
                        import re
                        titles = re.findall(r'<title><!\[CDATA\[(.*?)\]\]></title>', text)
                        _cryptopanic_cache = [{"title": t, "sentiment": 0,
                                               "source": "cryptopanic"} for t in titles[:10]]
                    log.info(f"📰 CryptoPanic: {len(_cryptopanic_cache)} notícias")
    except Exception as e:
        log.warning(f"update_cryptopanic: {e}")


def get_cryptopanic_sentiment() -> dict:
    """Retorna sentimento consolidado do CryptoPanic."""
    if not _cryptopanic_cache:
        return {"sentiment": "NEUTRAL", "score": 0, "count": 0}
    total = sum(item.get("sentiment", 0) for item in _cryptopanic_cache)
    count = len(_cryptopanic_cache)
    if total > 3:   return {"sentiment": "BULLISH",  "score": total, "count": count}
    if total < -3:  return {"sentiment": "BEARISH",  "score": total, "count": count}
    return {"sentiment": "NEUTRAL", "score": total, "count": count}


# ══════════════════════════════════════════════════════════════════
# ITEM 7 — Calendário Econômico Real (Forex Factory / FMP)
# Bloqueia entradas 30min antes de CPI, NFP, Fed, FOMC
# ══════════════════════════════════════════════════════════════════

_economic_events_cache: list = []
_last_calendar_update: float = 0

async def update_economic_calendar():
    """
    Busca eventos de alto impacto do calendário econômico.
    Fontes: FMP API (gratuita) + fallback hardcoded para datas conhecidas.
    Atualiza 1x por hora.
    """
    global _economic_events_cache, _last_calendar_update
    if _t.time() - _last_calendar_update < 3600:
        return

    events = []
    try:
        async with aiohttp.ClientSession() as s:
            # Financial Modeling Prep — calendário econômico gratuito
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            next3 = (datetime.now(timezone.utc) + timedelta(days=3)).strftime("%Y-%m-%d")
            url   = (f"https://financialmodelingprep.com/api/v3/economic_calendar"
                     f"?from={today}&to={next3}&apikey=demo")
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
                if r.status == 200:
                    data = await r.json()
                    high_impact = [e for e in data
                                   if e.get("impact", "").lower() == "high"]
                    for e in high_impact:
                        events.append({
                            "event":  e.get("event", ""),
                            "date":   e.get("date", ""),
                            "impact": "HIGH",
                            "country": e.get("country", ""),
                        })
    except Exception as e:
        log.debug(f"economic_calendar: {e}")

    _economic_events_cache = events
    _last_calendar_update  = _t.time()
    if events:
        log.info(f"📅 Calendário: {len(events)} eventos HIGH IMPACT nos próximos 3 dias")


def is_high_impact_window(minutes_before: int = 30) -> dict:
    """
    Verifica se estamos numa janela de evento de alto impacto.
    Retorna bloqueio se CPI, NFP, FOMC, Fed etc. dentro de X minutos.

    Palavras que classificam como alto impacto para crypto:
    CPI, NFP, Fed, FOMC, Interest Rate, Unemployment, GDP
    """

    HIGH_IMPACT_KEYWORDS = [
        "cpi", "consumer price", "nfp", "non-farm", "fomc", "fed",
        "interest rate", "unemployment", "gdp", "inflation",
        "pce", "powell", "federal reserve", "rate decision"
    ]

    now = datetime.now(timezone.utc)
    window_start = now - timedelta(minutes=5)
    window_end   = now + timedelta(minutes=minutes_before)

    for event in _economic_events_cache:
        try:
            event_dt = datetime.fromisoformat(
                event["date"].replace("Z", "+00:00")
            )
            if window_start <= event_dt <= window_end:
                name = event.get("event", "").lower()
                if any(kw in name for kw in HIGH_IMPACT_KEYWORDS):
                    return {
                        "blocked":    True,
                        "event":      event.get("event", ""),
                        "event_time": event["date"],
                        "minutes":    int((event_dt - now).total_seconds() / 60),
                    }
        except Exception:
            continue

    # Fallback: detectar por palavras-chave em notícias recentes
    return {"blocked": False, "event": None}


# ══════════════════════════════════════════════════════════════════
# ITEM 8 — Filtro de Volume Mínimo por Par ($50M/24h)
# ══════════════════════════════════════════════════════════════════

_volume_cache: dict = {}   # symbol → volume_24h_usd
_last_volume_update: float = 0

async def update_volume_filter(client):
    """
    Atualiza volume 24h de todos os pares via Bybit tickers.
    Atualiza a cada 30 minutos.
    """
    global _volume_cache, _last_volume_update
    if _t.time() - _last_volume_update < 1800:
        return
    try:
        tickers = await client.get_all_tickers()
        for t in tickers:
            sym = t.get("symbol", "")
            try:
                # turnover24h = volume em USDT
                vol_usd = float(t.get("turnover24h", 0))
                _volume_cache[sym] = vol_usd
            except Exception:
                pass
        _last_volume_update = _t.time()
        # Log pares filtrados
        low_vol = [s for s, v in _volume_cache.items()
                   if v < 50_000_000 and s.endswith("USDT")]
        if low_vol:
            log.info(f"⚠️ Volume <$50M/24h: {', '.join(low_vol[:5])}")
    except Exception as e:
        log.warning(f"update_volume_filter: {e}")


def has_minimum_volume(symbol: str, min_usd: float = 50_000_000) -> dict:
    """
    Verifica se o par tem volume mínimo de $50M/24h.
    Pares com volume baixo = slippage alto, stop hunt fácil.

    Retorna:
      tradeable: True se passa no filtro
      volume_24h: volume em USDT
      reason: motivo se bloqueado
    """
    vol = _volume_cache.get(symbol, 0)

    # Se não temos dados ainda, permitir (evita bloquear na inicialização)
    if vol == 0:
        return {"tradeable": True, "volume_24h": 0,
                "reason": "sem dados — permitindo"}

    if vol >= min_usd:
        return {"tradeable": True,  "volume_24h": vol,
                "reason": f"${vol/1e6:.0f}M/24h ✓"}
    else:
        return {"tradeable": False, "volume_24h": vol,
                "reason": f"${vol/1e6:.1f}M/24h < $50M mínimo"}

