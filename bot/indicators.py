"""
BGX Capital — Indicators v4.0
ADX, ATR, Bollinger Width, Choppiness Index,
EMA, RSI (completo), MACD, VWAP, Volume Profile,
SMC (BOS, CHoCH, HH/HL/LH/LL), Correlação entre pares
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
    """
    RSI COMPLETO — calcula todos os índices do array (não só o último).
    Permite: divergências, padrões históricos, cruzamentos de nível.
    v4.0: corrigido — antes apenas out[-1] era calculado, resto era 50.
    """
    arr    = np.array(closes, dtype=float)
    n      = len(arr)
    out    = np.full(n, 50.0)   # inicializa tudo com 50 (neutro)

    if n < period + 1:
        return out

    deltas = np.diff(arr)
    gains  = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)

    # Primeira média simples (Wilder)
    ag = gains[:period].mean()
    al = losses[:period].mean()

    def _rsi_val(ag, al):
        if al == 0:
            return 100.0
        return 100.0 - 100.0 / (1.0 + ag / al)

    out[period] = _rsi_val(ag, al)

    # Média suavizada para todos os índices seguintes
    for i in range(period, n - 1):
        ag = (ag * (period - 1) + gains[i])  / period
        al = (al * (period - 1) + losses[i]) / period
        out[i + 1] = _rsi_val(ag, al)

    return out


def macd(closes: list, fast=12, slow=26, signal=9):
    e_fast    = ema(closes, fast)
    e_slow    = ema(closes, slow)
    macd_line = e_fast - e_slow
    sig_line  = ema(macd_line.tolist(), signal)
    hist      = macd_line - sig_line
    return macd_line, sig_line, hist


def atr(highs: list, lows: list, closes: list, period: int = 14) -> np.ndarray:
    h  = np.array(highs,  dtype=float)
    l  = np.array(lows,   dtype=float)
    c  = np.array(closes, dtype=float)
    tr = np.zeros(len(c))
    tr[0] = h[0] - l[0]
    for i in range(1, len(c)):
        tr[i] = max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
    out = np.zeros(len(c))
    if len(c) >= period:
        out[period-1] = tr[:period].mean()
        for i in range(period, len(c)):
            out[i] = (out[i-1] * (period-1) + tr[i]) / period
    return out


# ── ADX ─────────────────────────────────────────────────────────
def adx(highs: list, lows: list, closes: list, period: int = 14) -> dict:
    h = np.array(highs,  dtype=float)
    l = np.array(lows,   dtype=float)
    c = np.array(closes, dtype=float)
    n = len(c)
    if n < period + 2:
        return {"adx": 0.0, "plus_di": 0.0, "minus_di": 0.0,
                "trending": False, "ranging": True, "direction": "LONG"}

    tr       = np.zeros(n)
    plus_dm  = np.zeros(n)
    minus_dm = np.zeros(n)
    for i in range(1, n):
        tr[i]       = max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
        up   = h[i] - h[i-1]
        down = l[i-1] - l[i]
        plus_dm[i]  = up   if (up > down and up > 0)   else 0
        minus_dm[i] = down if (down > up and down > 0) else 0

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
        pdi   = np.where(s_tr > 0, 100 * s_pdm / np.where(s_tr > 0, s_tr, 1), 0)
        mdi   = np.where(s_tr > 0, 100 * s_mdm / np.where(s_tr > 0, s_tr, 1), 0)
        denom = pdi + mdi
        dx    = np.where(denom > 0, 100 * np.abs(pdi-mdi) / np.where(denom > 0, denom, 1), 0)

    adx_arr = np.zeros(n)
    if n >= 2*period:
        adx_arr[2*period-1] = dx[period:2*period].mean()
        for i in range(2*period, n):
            adx_arr[i] = (adx_arr[i-1]*(period-1) + dx[i]) / period

    v     = float(adx_arr[-1])
    pdi_v = float(pdi[-1])
    mdi_v = float(mdi[-1])
    return {
        "adx":       round(v, 2),
        "plus_di":   round(pdi_v, 2),
        "minus_di":  round(mdi_v, 2),
        "trending":  v > 25,
        "ranging":   v < 20,
        "direction": "LONG" if pdi_v > mdi_v else "SHORT",
    }


# ── Bollinger Bands ──────────────────────────────────────────────
def bollinger(closes: list, period: int = 20, std_mult: float = 2.0) -> dict:
    arr = np.array(closes, dtype=float)
    if len(arr) < period:
        return {"upper": 0, "lower": 0, "mid": 0, "width": 0,
                "squeezed": False, "price_pct": 50}
    mid   = arr[-period:].mean()
    std   = arr[-period:].std()
    upper = mid + std_mult * std
    lower = mid - std_mult * std
    width = (upper - lower) / mid * 100 if mid > 0 else 0

    if len(arr) >= 50:
        widths = []
        for i in range(period, len(arr)):
            m = arr[i-period:i].mean()
            s = arr[i-period:i].std()
            widths.append((s * 2 * std_mult / m * 100) if m > 0 else 0)
        avg_width = np.mean(widths[-20:]) if widths else width
        squeezed  = width < avg_width * 0.7
    else:
        squeezed = width < 2.0

    return {
        "upper":     round(upper, 6),
        "lower":     round(lower, 6),
        "mid":       round(mid,   6),
        "width":     round(width, 3),
        "squeezed":  squeezed,
        "price_pct": round(
            (closes[-1] - lower) / (upper - lower) * 100, 1
        ) if (upper - lower) > 0 else 50,
    }


# ── Choppiness Index ─────────────────────────────────────────────
def choppiness(highs: list, lows: list, closes: list, period: int = 14) -> dict:
    if len(closes) < period + 1:
        return {"ci": 50.0, "trending": False, "chop": False, "neutral": True}

    h = np.array(highs[-period-1:],  dtype=float)
    l = np.array(lows[-period-1:],   dtype=float)
    c = np.array(closes[-period-1:], dtype=float)

    tr_sum = 0.0
    for i in range(1, period+1):
        tr_sum += max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))

    high_max = h[1:].max()
    low_min  = l[1:].min()
    hl_range = high_max - low_min

    if hl_range <= 0 or tr_sum <= 0:
        return {"ci": 50.0, "trending": False, "chop": False, "neutral": True}

    import math
    ci = 100 * math.log10(tr_sum / hl_range) / math.log10(period)

    return {
        "ci":       round(ci, 2),
        "trending": ci < 38.2,
        "chop":     ci > 61.8,
        "neutral":  38.2 <= ci <= 61.8,
    }


# ── VWAP ─────────────────────────────────────────────────────────
def vwap(highs: list, lows: list, closes: list, volumes: list) -> float:
    h   = np.array(highs,   dtype=float)
    l   = np.array(lows,    dtype=float)
    c   = np.array(closes,  dtype=float)
    v   = np.array(volumes, dtype=float)
    tp  = (h + l + c) / 3
    cum_tp_v = np.cumsum(tp * v)
    cum_v    = np.cumsum(v)
    vwap_arr = np.where(cum_v > 0, cum_tp_v / cum_v, c)
    return float(vwap_arr[-1])


# ── Volume Profile ────────────────────────────────────────────────
def volume_profile(highs: list, lows: list, volumes: list, bins: int = 20) -> dict:
    h   = np.array(highs,   dtype=float)
    l   = np.array(lows,    dtype=float)
    v   = np.array(volumes, dtype=float)
    lo  = l.min()
    hi  = h.max()
    if hi <= lo:
        return {"poc": (hi + lo) / 2, "vah": hi, "val": lo}
    edges  = np.linspace(lo, hi, bins + 1)
    vol_bins = np.zeros(bins)
    for i in range(len(h)):
        mid = (h[i] + l[i]) / 2
        idx = min(int((mid - lo) / (hi - lo) * bins), bins - 1)
        vol_bins[idx] += v[i]
    poc_idx = int(np.argmax(vol_bins))
    poc     = (edges[poc_idx] + edges[poc_idx+1]) / 2
    cum     = np.cumsum(vol_bins)
    total   = cum[-1]
    vah_idx = np.searchsorted(cum, total * 0.70)
    val_idx = np.searchsorted(cum, total * 0.30)
    return {
        "poc": round(float(poc), 6),
        "vah": round(float(edges[min(vah_idx+1, bins)]), 6),
        "val": round(float(edges[val_idx]),              6),
    }


# ── Orderbook Imbalance ───────────────────────────────────────────
def orderbook_imbalance(ob: dict) -> dict:
    if not ob:
        return {"bias": "NEUTRAL", "ratio": 1.0}
    bids = ob.get("b", [])
    asks = ob.get("a", [])
    bid_vol = sum(float(b[1]) for b in bids[:10] if len(b) >= 2)
    ask_vol = sum(float(a[1]) for a in asks[:10] if len(a) >= 2)
    total   = bid_vol + ask_vol
    if total <= 0:
        return {"bias": "NEUTRAL", "ratio": 1.0}
    ratio = bid_vol / ask_vol if ask_vol > 0 else 2.0
    if ratio > 1.5:   bias = "BID_HEAVY"
    elif ratio < 0.67: bias = "ASK_HEAVY"
    else:              bias = "NEUTRAL"
    return {"bias": bias, "ratio": round(ratio, 3),
            "bid_vol": round(bid_vol, 2), "ask_vol": round(ask_vol, 2)}


# ── Delta Footprint ───────────────────────────────────────────────
def delta_footprint(closes: list, volumes: list, opens: list) -> dict:
    if len(closes) < 5:
        return {"bias": "NEUTRAL", "divergence": False, "delta": 0.0}
    c    = np.array(closes[-5:],  dtype=float)
    v    = np.array(volumes[-5:], dtype=float)
    o    = np.array(opens[-5:],   dtype=float)
    body = c - o
    buy_v  = v * np.where(body >= 0, 1.0, 0.3)
    sell_v = v * np.where(body <  0, 1.0, 0.3)
    delta  = float(buy_v.sum() - sell_v.sum())
    price_up   = c[-1] > c[-2]
    delta_down = delta < 0
    divergence = price_up and delta_down
    bias = "BULLISH" if delta > 0 else "BEARISH" if delta < 0 else "NEUTRAL"
    return {"bias": bias, "divergence": divergence, "delta": round(delta, 2)}


# ── SMC Analysis ─────────────────────────────────────────────────
def smc_analysis(highs: list, lows: list, closes: list) -> dict:
    result = {
        "structure": "UNKNOWN", "hh": False, "hl": False,
        "lh": False, "ll": False, "bos": False,
        "bos_dir": "NONE", "choch": False,
    }
    if len(closes) < 10:
        return result

    h = highs[-10:]
    l = lows[-10:]
    c = closes[-10:]

    prev_h = max(h[:-3])
    prev_l = min(l[:-3])
    cur_h  = h[-1]
    cur_l  = l[-1]

    hh = cur_h > prev_h
    ll = cur_l < prev_l
    hl = cur_l > prev_l
    lh = cur_h < prev_h

    # BOS — Break of Structure
    bos, bos_dir = False, "NONE"
    if hh:
        bos     = True
        bos_dir = "LONG"
    elif ll:
        bos     = True
        bos_dir = "SHORT"

    # CHoCH — Change of Character
    choch = (hh and ll) or (hl and lh)

    if hh and hl:   structure = "BULLISH"
    elif ll and lh: structure = "BEARISH"
    elif choch:     structure = "REVERSAL"
    else:           structure = "NEUTRAL"

    result.update({
        "structure": structure, "hh": hh, "hl": hl,
        "lh": lh, "ll": ll, "bos": bos,
        "bos_dir": bos_dir, "choch": choch,
    })
    return result


# ── Correlação entre pares ────────────────────────────────────────
def pearson_correlation(series_a: list, series_b: list, period: int = 20) -> float:
    """
    Calcula correlação de Pearson entre dois séries de fechamento.
    Usado para bloquear abertura de par altamente correlacionado com posição aberta.
    Retorna valor entre -1 e 1 (> 0.70 = correlação alta).
    """
    if len(series_a) < period or len(series_b) < period:
        return 0.0
    a = np.array(series_a[-period:], dtype=float)
    b = np.array(series_b[-period:], dtype=float)
    if a.std() == 0 or b.std() == 0:
        return 0.0
    return float(np.corrcoef(a, b)[0, 1])


def returns_correlation(closes_a: list, closes_b: list, period: int = 20) -> float:
    """
    Correlação sobre retornos percentuais (mais estável que sobre preços).
    """
    if len(closes_a) < period + 1 or len(closes_b) < period + 1:
        return 0.0
    a = np.array(closes_a[-(period+1):], dtype=float)
    b = np.array(closes_b[-(period+1):], dtype=float)
    ret_a = np.diff(a) / a[:-1]
    ret_b = np.diff(b) / b[:-1]
    if ret_a.std() == 0 or ret_b.std() == 0:
        return 0.0
    return float(np.corrcoef(ret_a, ret_b)[0, 1])


# ── Orderbook history helper ──────────────────────────────────────
_ob_history: dict = {}

def update_orderbook_history(symbol: str, ob: dict):
    if symbol not in _ob_history:
        _ob_history[symbol] = []
    _ob_history[symbol].append(ob)
    if len(_ob_history[symbol]) > 10:
        _ob_history[symbol].pop(0)

def get_orderbook_history(symbol: str) -> list:
    return _ob_history.get(symbol, [])
