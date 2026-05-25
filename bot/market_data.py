"""
AA Capital — Market Data Module
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
