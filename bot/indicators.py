"""
BGX Capital — Indicators v3.0
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

# ══════════════════════════════════════════════════════════════════
# ORDER FLOW AVANÇADO — Nível Institucional
# ══════════════════════════════════════════════════════════════════

# ── Spoofing Detector ─────────────────────────────────────────────
# Histórico de snapshots do orderbook por símbolo
_ob_history: dict = {}   # symbol → lista de snapshots {ts, bids, asks}
_OB_HISTORY_MAX = 20     # manter últimos 20 snapshots (~10 segundos)

def update_orderbook_history(symbol: str, orderbook: dict):
    """
    Armazena snapshot do orderbook para análise de spoofing.
    Deve ser chamado a cada atualização do WebSocket (~500ms).
    """
    import time
    if symbol not in _ob_history:
        _ob_history[symbol] = []
    snapshot = {
        "ts":   time.time(),
        "bids": {float(b[0]): float(b[1]) for b in orderbook.get("b", [])[:20]},
        "asks": {float(a[0]): float(a[1]) for a in orderbook.get("a", [])[:20]},
    }
    _ob_history[symbol].append(snapshot)
    if len(_ob_history[symbol]) > _OB_HISTORY_MAX:
        _ob_history[symbol].pop(0)


def detect_spoofing(symbol: str) -> dict:
    """
    Spoofing Detector — detecta ordens grandes que aparecem e somem.

    Como funciona:
    - Compara snapshots consecutivos do orderbook
    - Se uma ordem grande (>= 3x média) aparece e desaparece
      em menos de 3 snapshots SEM execução → spoofing detectado

    Retorna:
      spoofing_bid: True se há spoofing no lado comprador (falsa pressão de compra)
      spoofing_ask: True se há spoofing no lado vendedor (falsa pressão de venda)
      score_penalty: penalidade sugerida no score (-10 a -25)
    """
    import numpy as np
    history = _ob_history.get(symbol, [])
    result  = {"spoofing_bid": False, "spoofing_ask": False,
                "score_penalty": 0, "detail": ""}

    if len(history) < 4:
        return result

    # Para cada lado (bid/ask), verificar ordens que aparecem e somem
    for side in ["bids", "asks"]:
        all_vols = []
        for snap in history:
            all_vols.extend(snap[side].values())
        if not all_vols:
            continue

        mean_vol = float(np.mean(all_vols)) if all_vols else 1.0
        threshold = mean_vol * 3.0   # ordem "grande" = 3x a média

        # Verificar se ordem grande apareceu e desapareceu
        for i in range(1, len(history) - 1):
            prev_snap = history[i - 1][side]
            curr_snap = history[i][side]
            next_snap = history[i + 1][side]

            for price, vol in curr_snap.items():
                if vol >= threshold:
                    # Estava presente antes?
                    existed_before = price in prev_snap and prev_snap[price] >= threshold * 0.5
                    # Sumiu depois?
                    gone_after     = price not in next_snap or next_snap[price] < vol * 0.3

                    if not existed_before and gone_after:
                        # Apareceu grande e sumiu rápido = spoofing
                        if side == "bids":
                            result["spoofing_bid"]   = True
                            result["score_penalty"] -= 15
                            result["detail"] += f"SPOOF_BID@{price:.2f}({vol:.0f}) "
                        else:
                            result["spoofing_ask"]   = True
                            result["score_penalty"] -= 15
                            result["detail"] += f"SPOOF_ASK@{price:.2f}({vol:.0f}) "

    return result


# ── Iceberg Detector ──────────────────────────────────────────────
def detect_iceberg(symbol: str, price: float) -> dict:
    """
    Iceberg Detector — detecta ordens ocultas que se repõem no mesmo nível.

    Como funciona:
    - Monitora um nível de preço específico
    - Se o volume desaparece (execução) mas o nível se repõe rapidamente
      com volume similar → iceberg (ordem grande oculta se desmembrando)

    Sinais de iceberg:
      - Mesmo preço reabastecido 3+ vezes consecutivas
      - Volume reposto similar ao original (±20%)
    """
    history = _ob_history.get(symbol, [])
    result  = {"iceberg_bid": False, "iceberg_ask": False,
                "iceberg_price": 0.0, "iceberg_side": "", "detail": ""}

    if len(history) < 5:
        return result

    # Analisar níveis de preço próximos ao preço atual (±0.5%)
    price_range = price * 0.005

    for side in ["bids", "asks"]:
        level_counts: dict = {}   # price → lista de volumes observados

        for snap in history:
            for p, v in snap[side].items():
                if abs(p - price) <= price_range and v > 0:
                    if p not in level_counts:
                        level_counts[p] = []
                    level_counts[p].append(v)

        for p, vols in level_counts.items():
            if len(vols) < 4:
                continue
            # Verifica se volume oscila (some e volta) com valor similar
            reloads = 0
            for i in range(1, len(vols)):
                dropped  = vols[i] < vols[i-1] * 0.4   # volume caiu >60%
                reloaded = vols[i] > vols[i-1] * 0.6   # volume voltou
                if i >= 2 and dropped and reloaded:
                    reloads += 1

            if reloads >= 2:
                if side == "bids":
                    result["iceberg_bid"]   = True
                    result["iceberg_price"] = p
                    result["iceberg_side"]  = "BID"
                    result["detail"]       += f"ICEBERG_BID@{p:.2f}(reloads={reloads}) "
                else:
                    result["iceberg_ask"]   = True
                    result["iceberg_price"] = p
                    result["iceberg_side"]  = "ASK"
                    result["detail"]       += f"ICEBERG_ASK@{p:.2f}(reloads={reloads}) "

    return result


# ── Agressão Compradora / Vendedora ───────────────────────────────
def detect_aggression(trades: list) -> dict:
    """
    Detecta agressão compradora ou vendedora com base nos últimos trades.

    'trades' = lista de trades recentes da exchange:
    [{"price": 1.23, "qty": 100, "side": "Buy"/"Sell", "ts": 123456}, ...]

    Agressão compradora: sequência de market buys grandes e rápidos
    Agressão vendedora:  sequência de market sells grandes e rápidos

    Retorna:
      aggressor:      "BUYER" | "SELLER" | "NEUTRAL"
      buy_ratio:      % de volume que foi compra agressiva
      sell_ratio:     % de volume que foi venda agressiva
      momentum:       "ACCELERATING" | "DECELERATING" | "STABLE"
    """
    import numpy as np

    if not trades or len(trades) < 5:
        return {"aggressor": "NEUTRAL", "buy_ratio": 0.5,
                "sell_ratio": 0.5, "momentum": "STABLE", "detail": ""}

    buy_vol  = sum(float(t.get("qty", t.get("size", 0)))
                   for t in trades if t.get("side", "").lower() in ("buy", "b"))
    sell_vol = sum(float(t.get("qty", t.get("size", 0)))
                   for t in trades if t.get("side", "").lower() in ("sell", "s"))
    total    = buy_vol + sell_vol or 1

    buy_ratio  = buy_vol  / total
    sell_ratio = sell_vol / total

    # Detectar aceleração: últimos 30% dos trades vs primeiros 30%
    n     = len(trades)
    early = trades[:n//3]
    late  = trades[-(n//3):]

    early_buy = sum(float(t.get("qty", 0)) for t in early
                    if t.get("side","").lower() in ("buy","b"))
    late_buy  = sum(float(t.get("qty", 0)) for t in late
                    if t.get("side","").lower() in ("buy","b"))

    if early_buy > 0:
        momentum_ratio = late_buy / early_buy
        momentum = ("ACCELERATING" if momentum_ratio > 1.3
                    else "DECELERATING" if momentum_ratio < 0.7
                    else "STABLE")
    else:
        momentum = "STABLE"

    if buy_ratio >= 0.65:
        aggressor = "BUYER"
    elif sell_ratio >= 0.65:
        aggressor = "SELLER"
    else:
        aggressor = "NEUTRAL"

    return {
        "aggressor":   aggressor,
        "buy_ratio":   round(buy_ratio,  3),
        "sell_ratio":  round(sell_ratio, 3),
        "momentum":    momentum,
        "buy_vol":     round(buy_vol,  2),
        "sell_vol":    round(sell_vol, 2),
        "detail":      f"{aggressor} buy={buy_ratio:.0%} sell={sell_ratio:.0%} {momentum}",
    }


# ── Absorção ─────────────────────────────────────────────────────
def detect_absorption(symbol: str, closes: list, volumes: list,
                       orderbook: dict) -> dict:
    """
    Absorção — grande player absorve pressão do lado oposto sem mover preço.

    Cenário BULLISH (absorção de venda):
    - Alto volume de venda chegando no bid
    - Preço não cai (ou cai minimamente)
    - Bid se mantém firme → buyer absorvendo todo sell

    Cenário BEARISH (absorção de compra):
    - Alto volume de compra chegando no ask
    - Preço não sobe (ou sobe minimamente)
    - Ask se mantém firme → seller absorvendo todo buy

    Retorna:
      absorption_bull: True = buyer absorvendo vendas (sinal de alta)
      absorption_bear: True = seller absorvendo compras (sinal de baixa)
      strength:        0.0 - 1.0
    """
    import numpy as np

    result = {"absorption_bull": False, "absorption_bear": False,
               "strength": 0.0, "detail": ""}

    if len(closes) < 5 or len(volumes) < 5:
        return result

    closes_arr  = np.array(closes[-10:],  dtype=float)
    volumes_arr = np.array(volumes[-10:], dtype=float)

    price_change   = abs(closes_arr[-1] - closes_arr[0]) / closes_arr[0] if closes_arr[0] > 0 else 1
    avg_vol        = float(np.mean(volumes_arr[:-1])) or 1
    recent_vol     = float(volumes_arr[-1])
    vol_ratio      = recent_vol / avg_vol

    # Alto volume mas preço não se move = absorção
    if vol_ratio >= 2.0 and price_change < 0.003:   # vol 2x+ mas movimento < 0.3%
        # Determinar direção pela pressão do orderbook
        ob_result = orderbook_imbalance(orderbook) if orderbook else {"imbalance": 0.5}
        imbalance = ob_result.get("imbalance", 0.5)

        strength = min(1.0, (vol_ratio - 2.0) / 3.0 + 0.3)

        if imbalance < 0.4:
            # Mais asks que bids mas preço não cai = buyer absorvendo
            result["absorption_bull"] = True
            result["strength"]        = round(strength, 2)
            result["detail"]          = f"ABSORB_BULL vol={vol_ratio:.1f}x Δprice={price_change:.4%}"
        elif imbalance > 0.6:
            # Mais bids que asks mas preço não sobe = seller absorvendo
            result["absorption_bear"] = True
            result["strength"]        = round(strength, 2)
            result["detail"]          = f"ABSORB_BEAR vol={vol_ratio:.1f}x Δprice={price_change:.4%}"

    return result

