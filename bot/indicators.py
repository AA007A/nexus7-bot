"""
AA Capital — Indicators v3.0
ADX, ATR, Bollinger Width, Choppiness Index,
EMA, RSI, MACD, VWAP, Volume Profile,
SMC (BOS, CHoCH, HH/HL/LH/LL)
"""
import numpy as np
from typing import Optional


def ema(closes: list, period: int) -> np.ndarray:
    arr = np.array(closes, dtype=float)
    k   = 2.0 / (period + 1)
    out = np.zeros_like(arr)
    out[0] = arr[0]
    for i in range(1, len(arr)):
        out[i] = arr[i] * k + out[i-1] * (1 - k)
    return out


def rsi(closes: list, period: int = 14) -> np.ndarray:
    arr    = np.array(closes, dtype=float)
    deltas = np.diff(arr)
    gains  = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    out    = np.zeros(len(arr))
    if len(gains) < period:
        out[:] = 50.0
        return out
    ag = gains[:period].mean()
    al = losses[:period].mean()
    for i in range(period, len(deltas)):
        ag = (ag * (period - 1) + gains[i])  / period
        al = (al * (period - 1) + losses[i]) / period
    rs = ag / al if al > 0 else 1e9
    out[-1] = 100 - 100 / (1 + rs)
    out[:-1] = 50.0
    return out


def macd(closes: list, fast=12, slow=26, signal=9):
    e_fast   = ema(closes, fast)
    e_slow   = ema(closes, slow)
    macd_line= e_fast - e_slow
    sig_line = ema(macd_line.tolist(), signal)
    hist     = macd_line - sig_line
    return macd_line, sig_line, hist


def atr(highs: list, lows: list, closes: list, period: int = 14) -> np.ndarray:
    h = np.array(highs,  dtype=float)
    l = np.array(lows,   dtype=float)
    c = np.array(closes, dtype=float)
    tr = np.zeros(len(c))
    tr[0] = h[0] - l[0]
    for i in range(1, len(c)):
        tr[i] = max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
    out = np.zeros(len(c))
    out[period-1] = tr[:period].mean()
    for i in range(period, len(c)):
        out[i] = (out[i-1] * (period-1) + tr[i]) / period
    return out


# ── ADX ─────────────────────────────────────────────────────────
def adx(highs: list, lows: list, closes: list, period: int = 14) -> dict:
    """
    ADX — mede FORÇA da tendência (não direção).
    > 25 → tendência válida
    < 20 → mercado lateral → NÃO operar
    Retorna: adx_val, plus_di, minus_di
    """
    h = np.array(highs,  dtype=float)
    l = np.array(lows,   dtype=float)
    c = np.array(closes, dtype=float)
    n = len(c)
    if n < period + 2:
        return {"adx": 0.0, "plus_di": 0.0, "minus_di": 0.0, "trending": False}

    # True Range
    tr = np.zeros(n)
    plus_dm  = np.zeros(n)
    minus_dm = np.zeros(n)
    for i in range(1, n):
        tr[i] = max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
        up   = h[i] - h[i-1]
        down = l[i-1] - l[i]
        plus_dm[i]  = up   if (up > down and up > 0)   else 0
        minus_dm[i] = down if (down > up and down > 0) else 0

    # Smoothed
    def smooth(arr, p):
        out = np.zeros(n)
        out[p] = arr[1:p+1].sum()
        for i in range(p+1, n):
            out[i] = out[i-1] - out[i-1]/p + arr[i]
        return out

    s_tr  = smooth(tr,       period)
    s_pdm = smooth(plus_dm,  period)
    s_mdm = smooth(minus_dm, period)

    with np.errstate(divide='ignore', invalid='ignore'):
        pdi = np.where(s_tr > 0, 100 * s_pdm / np.where(s_tr > 0, s_tr, 1), 0)
        mdi = np.where(s_tr > 0, 100 * s_mdm / np.where(s_tr > 0, s_tr, 1), 0)
        denom = pdi + mdi
        dx  = np.where(denom > 0, 100 * np.abs(pdi-mdi) / np.where(denom > 0, denom, 1), 0)

    adx_arr = np.zeros(n)
    adx_arr[2*period-1] = dx[period:2*period].mean()
    for i in range(2*period, n):
        adx_arr[i] = (adx_arr[i-1]*(period-1) + dx[i]) / period

    v      = float(adx_arr[-1])
    pdi_v  = float(pdi[-1])
    mdi_v  = float(mdi[-1])
    return {
        "adx":       round(v, 2),
        "plus_di":   round(pdi_v, 2),
        "minus_di":  round(mdi_v, 2),
        "trending":  v > 25,
        "ranging":   v < 20,
        "direction": "LONG" if pdi_v > mdi_v else "SHORT",
    }


# ── Bollinger Bands + Band Width ─────────────────────────────────
def bollinger(closes: list, period: int = 20, std_mult: float = 2.0) -> dict:
    """
    Bollinger Band Width: detecta compressão ANTES de expansão violenta.
    Width baixo → squeeze → breakout iminente.
    """
    arr = np.array(closes, dtype=float)
    if len(arr) < period:
        return {"upper": 0, "lower": 0, "mid": 0, "width": 0, "squeezed": False}
    mid   = arr[-period:].mean()
    std   = arr[-period:].std()
    upper = mid + std_mult * std
    lower = mid - std_mult * std
    width = (upper - lower) / mid * 100 if mid > 0 else 0

    # Squeeze: width atual vs média histórica das últimas 50 velas
    if len(arr) >= 50:
        widths = []
        for i in range(period, len(arr)):
            m = arr[i-period:i].mean()
            s = arr[i-period:i].std()
            widths.append((s*2*std_mult/m*100) if m > 0 else 0)
        avg_width = np.mean(widths[-20:]) if widths else width
        squeezed  = width < avg_width * 0.7   # width 30% abaixo da média = squeeze
    else:
        squeezed = width < 2.0

    return {
        "upper":    round(upper, 6),
        "lower":    round(lower, 6),
        "mid":      round(mid,   6),
        "width":    round(width, 3),
        "squeezed": squeezed,
        "price_pct": round((closes[-1] - lower) / (upper - lower) * 100, 1) if (upper-lower) > 0 else 50,
    }


# ── Choppiness Index ─────────────────────────────────────────────
def choppiness(highs: list, lows: list, closes: list, period: int = 14) -> dict:
    """
    Choppiness Index: filtra falso breakout.
    > 61.8 → mercado choppiness/lateral → NÃO operar
    < 38.2 → mercado trending forte → operar
    Entre 38.2-61.8 → neutro
    """
    if len(closes) < period + 1:
        return {"ci": 50.0, "trending": False, "chop": False}

    h = np.array(highs[-period-1:],  dtype=float)
    l = np.array(lows[-period-1:],   dtype=float)
    c = np.array(closes[-period-1:], dtype=float)

    # ATR sum
    tr_sum = 0
    for i in range(1, period+1):
        tr_sum += max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))

    high_max = h[1:].max()
    low_min  = l[1:].min()
    hl_range = high_max - low_min

    if hl_range <= 0 or tr_sum <= 0:
        return {"ci": 50.0, "trending": False, "chop": False}

    import math
    ci = 100 * math.log10(tr_sum / hl_range) / math.log10(period)

    return {
        "ci":       round(ci, 2),
        "trending": ci < 38.2,
        "chop":     ci > 61.8,
        "neutral":  38.2 <= ci <= 61.8,
    }


# ── VWAP ────────────────────────────────────────────────────────
def vwap(highs: list, lows: list, closes: list, volumes: list) -> float:
    """
    VWAP — Volume Weighted Average Price.
    Preço acima do VWAP = bullish bias.
    Preço abaixo = bearish bias.
    """
    h = np.array(highs,   dtype=float)
    l = np.array(lows,    dtype=float)
    c = np.array(closes,  dtype=float)
    v = np.array(volumes, dtype=float)
    typical = (h + l + c) / 3
    return float(np.sum(typical * v) / np.sum(v)) if np.sum(v) > 0 else float(c[-1])


# ── Volume Profile ───────────────────────────────────────────────
def volume_profile(highs: list, lows: list, volumes: list, bins: int = 10) -> dict:
    """
    Volume Profile: identifica POC (Point of Control) e Value Area.
    POC = nível com maior volume negociado.
    """
    if len(highs) < 5:
        return {"poc": 0, "vah": 0, "val": 0}

    h = np.array(highs,   dtype=float)
    l = np.array(lows,    dtype=float)
    v = np.array(volumes, dtype=float)

    min_p = l.min()
    max_p = h.max()
    if max_p <= min_p:
        return {"poc": float(h[-1]), "vah": float(h[-1]), "val": float(l[-1])}

    step   = (max_p - min_p) / bins
    levels = np.zeros(bins)
    prices = np.linspace(min_p + step/2, max_p - step/2, bins)

    for i in range(len(h)):
        for b in range(bins):
            low_b  = min_p + b * step
            high_b = low_b + step
            if l[i] <= high_b and h[i] >= low_b:
                levels[b] += v[i]

    poc_idx = int(np.argmax(levels))
    poc     = float(prices[poc_idx])

    # Value Area (70% do volume)
    total_vol = levels.sum()
    target    = total_vol * 0.70
    sorted_idx= np.argsort(levels)[::-1]
    accum = 0; va_bins = []
    for idx in sorted_idx:
        accum += levels[idx]
        va_bins.append(idx)
        if accum >= target:
            break

    vah = float(prices[max(va_bins)])
    val = float(prices[min(va_bins)])

    return {"poc": round(poc, 4), "vah": round(vah, 4), "val": round(val, 4)}


# ── Order Book Imbalance ────────────────────────────────────────
def orderbook_imbalance(orderbook: dict) -> dict:
    """
    Detecta desequilíbrio entre bids e asks.
    Imbalance > 60% bid = pressão compradora.
    Imbalance > 60% ask = pressão vendedora.
    """
    if not orderbook:
        return {"imbalance": 0.5, "bias": "NEUTRAL", "bid_vol": 0, "ask_vol": 0}

    bids = orderbook.get("b", [])
    asks = orderbook.get("a", [])

    bid_vol = sum(float(b[1]) for b in bids[:10])
    ask_vol = sum(float(a[1]) for a in asks[:10])
    total   = bid_vol + ask_vol

    if total <= 0:
        return {"imbalance": 0.5, "bias": "NEUTRAL", "bid_vol": 0, "ask_vol": 0}

    imb = bid_vol / total
    bias = "BID_HEAVY" if imb > 0.6 else ("ASK_HEAVY" if imb < 0.4 else "NEUTRAL")

    return {
        "imbalance": round(imb, 3),
        "bias":      bias,
        "bid_vol":   round(bid_vol, 2),
        "ask_vol":   round(ask_vol, 2),
    }


# ── Delta Footprint ─────────────────────────────────────────────
def delta_footprint(closes: list, volumes: list, opens: list) -> dict:
    """
    Delta Footprint: diferença entre volume de compra e venda por candle.
    Delta positivo = buyers dominam.
    Delta negativo = sellers dominam.
    Divergência preço sobe + delta cai = fraqueza.
    """
    if len(closes) < 5:
        return {"delta": 0, "cumulative_delta": 0, "divergence": False}

    deltas = []
    for i in range(len(closes)):
        body = closes[i] - opens[i]
        vol  = volumes[i] if i < len(volumes) else 0
        # Estimativa: se vela de alta → maioria buy volume
        if body > 0:
            buy_vol  = vol * 0.7
            sell_vol = vol * 0.3
        elif body < 0:
            buy_vol  = vol * 0.3
            sell_vol = vol * 0.7
        else:
            buy_vol = sell_vol = vol * 0.5
        deltas.append(buy_vol - sell_vol)

    cum_delta = float(np.sum(deltas))
    last_delta= float(deltas[-1]) if deltas else 0

    # Divergência: preço sobe mas delta cai (ou vice versa)
    price_dir = closes[-1] > closes[-5] if len(closes) >= 5 else True
    delta_dir = cum_delta > 0
    divergence= price_dir != delta_dir

    return {
        "delta":            round(last_delta, 2),
        "cumulative_delta": round(cum_delta, 2),
        "divergence":       divergence,
        "bias":             "BULLISH" if cum_delta > 0 else "BEARISH",
    }


# ── SMC — Smart Money Concepts ───────────────────────────────────
def smc_analysis(highs: list, lows: list, closes: list) -> dict:
    """
    Detecta automaticamente:
    - Higher Highs (HH) / Higher Lows (HL) → uptrend
    - Lower Highs (LH) / Lower Lows (LL)   → downtrend
    - Break of Structure (BOS)
    - Change of Character (CHoCH)
    """
    if len(highs) < 10:
        return {"structure": "UNKNOWN", "bos": False, "choch": False,
                "hh": False, "hl": False, "lh": False, "ll": False}

    h = highs[-20:]
    l = lows[-20:]
    c = closes[-20:]

    # Pivots locais (simplificado: cada 4 velas)
    pivot_highs = [max(h[i:i+4]) for i in range(0, len(h)-3, 4)]
    pivot_lows  = [min(l[i:i+4]) for i in range(0, len(l)-3, 4)]

    hh = False; hl = False; lh = False; ll = False
    if len(pivot_highs) >= 2:
        hh = pivot_highs[-1] > pivot_highs[-2]
        lh = pivot_highs[-1] < pivot_highs[-2]
    if len(pivot_lows) >= 2:
        hl = pivot_lows[-1] > pivot_lows[-2]
        ll = pivot_lows[-1] < pivot_lows[-2]

    # Estrutura
    if hh and hl:
        structure = "UPTREND"
    elif lh and ll:
        structure = "DOWNTREND"
    elif hh and ll:
        structure = "DISTRIBUTION"
    elif lh and hl:
        structure = "ACCUMULATION"
    else:
        structure = "RANGING"

    # BOS: preço fecha acima do último swing high (bull BOS)
    # ou abaixo do último swing low (bear BOS)
    last_swing_high = max(h[:-3]) if len(h) > 3 else h[-1]
    last_swing_low  = min(l[:-3]) if len(l) > 3 else l[-1]
    bull_bos = c[-1] > last_swing_high
    bear_bos = c[-1] < last_swing_low
    bos      = bull_bos or bear_bos
    bos_dir  = "BULLISH" if bull_bos else ("BEARISH" if bear_bos else "NONE")

    # CHoCH: mudança de caráter (primeiro BOS contra a tendência)
    choch = (structure == "UPTREND" and bear_bos) or (structure == "DOWNTREND" and bull_bos)

    return {
        "structure": structure,
        "hh": hh, "hl": hl, "lh": lh, "ll": ll,
        "bos":     bos,
        "bos_dir": bos_dir,
        "choch":   choch,
        "last_swing_high": round(last_swing_high, 6),
        "last_swing_low":  round(last_swing_low, 6),
    }
