"""
KAKAZITO TRADE Strategy v11.0
Multi-Timeframe: 4H → 1H → 15M
Entrada antecipada: BOS_BREAK > MOMENTUM > PULLBACK
Indicadores: ADX, BB Width, Choppiness, VWAP, SMC, Delta, OB Imbalance
"""
import numpy as np
from dataclasses import dataclass, field
from typing import Optional, Tuple
from bot.indicators import (
    ema, rsi, macd, atr,
    adx as adx_fn, bollinger, choppiness as chop_fn,
    vwap as vwap_fn, volume_profile,
    orderbook_imbalance, delta_footprint, smc_analysis,
)
from bot.logger import log

TAKER_FEE   = 0.00055
SLIPPAGE    = 0.00020
FUNDING_FEE = 0.00010
TOTAL_COST  = (TAKER_FEE + SLIPPAGE) * 2 + FUNDING_FEE


@dataclass
class Signal:
    symbol:       str
    direction:    str
    entry:        float
    sl:           float
    tp:           float
    confidence:   float
    reason:       str  = ""
    score:        int  = 0
    tf_4h:        str  = ""
    tf_1h:        str  = ""
    tf_15m:       str  = ""
    expected_pnl: float = 0.0
    total_fees:   float = 0.0
    entry_type:   str  = "PULLBACK"
    rr:           float = field(init=False)

    def __post_init__(self):
        risk   = abs(self.entry - self.sl)
        reward = abs(self.tp - self.entry)
        self.rr = round(reward / risk, 2) if risk > 0 else 0


# ─── Regime Detector ─────────────────────────────────────────────
def detect_regime(closes, highs, lows, atr_v) -> str:
    if len(closes) < 20:
        return "UNKNOWN"
    price   = closes[-1]
    atr_arr = atr(highs, lows, closes)
    atr_avg = float(np.mean(atr_arr[-20:])) if len(atr_arr) >= 20 else atr_v

    # Choppiness + ADX como proxy de regime
    try:
        ci  = chop_fn(highs, lows, closes)
        adx_data = adx_fn(highs, lows, closes)
        if atr_v < atr_avg * 0.65:
            return "COMPRESSED"
        if ci["chop"] and adx_data["adx"] < 20:
            return "RANGING"
        if adx_data["trending"] and adx_data["adx"] > 25:
            e20 = ema(closes, 20)[-1]
            return "TRENDING_UP" if price > e20 else "TRENDING_DOWN"
        return "RANGING"
    except Exception:
        e20 = ema(closes, 20)[-1]
        e50 = ema(closes, 50)[-1]
        if e20 > e50 and price > e20: return "TRENDING_UP"
        if e20 < e50 and price < e20: return "TRENDING_DOWN"
        return "RANGING"


# ─── Entry Type Detector ─────────────────────────────────────────
def detect_entry(closes, highs, lows, opens, volumes, direction, atr_v) -> Tuple[bool, str]:
    """
    Detecta o melhor tipo de entrada (do mais antecipado ao mais conservador).
    BOS_BREAK → MOMENTUM → PULLBACK → NONE
    """
    if len(closes) < 6:
        return False, "NONE"

    price = closes[-1]

    # 1) BOS Entry: preço fecha acima do swing high (LONG) ou abaixo do swing low (SHORT)
    # É a entrada MAIS ANTECIPADA — pega o início do movimento
    swing_high = max(highs[-8:-1]) if len(highs) > 8 else highs[-2]
    swing_low  = min(lows[-8:-1])  if len(lows)  > 8 else lows[-2]

    if direction == "LONG"  and price > swing_high: return True, "BOS_BREAK"
    if direction == "SHORT" and price < swing_low:  return True, "BOS_BREAK"

    # 2) Momentum: vela forte na direção sem esperar pullback
    body_size = abs(closes[-1] - opens[-1]) if opens else abs(closes[-1] - closes[-2])
    if direction == "LONG"  and closes[-1] > closes[-2] and body_size > atr_v * 0.25:
        return True, "MOMENTUM"
    if direction == "SHORT" and closes[-1] < closes[-2] and body_size > atr_v * 0.25:
        return True, "MOMENTUM"

    # 3) Pullback clássico
    if direction == "LONG":
        local_min = min(lows[-6:])
        if ((max(highs[-6:]) - local_min) >= atr_v * 0.35
                and price > local_min + atr_v * 0.12):
            return True, "PULLBACK"
    else:
        local_max = max(highs[-6:])
        if ((local_max - min(lows[-6:])) >= atr_v * 0.35
                and price < local_max - atr_v * 0.12):
            return True, "PULLBACK"

    return False, "NONE"


# ─── Score de Confluência ─────────────────────────────────────────
def score_tf(closes, highs, lows, opens, volumes, direction,
             atr_v, atr_avg, orderbook=None) -> dict:
    """
    Score 0-100 por timeframe.
    TENDÊNCIA   30pts: ADX + EMA + SMC (HH/HL, BOS)
    VOLUME      20pts: vol ratio + VWAP + OB imbalance
    MOMENTUM    20pts: RSI + MACD + Delta footprint
    VOLATILIDADE15pts: ATR + BB Width + Choppiness
    ESTRUTURA   15pts: SMC struct + anti-choch + anti-fake
    """
    price = closes[-1]
    if price <= 0 or atr_v <= 0:
        return {"ok": False, "total": 0}

    vols    = np.array(volumes, dtype=float)
    avg_vol = vols[-21:-1].mean() if len(vols) > 21 else (vols.mean() or 1)
    vol_r   = float(vols[-1] / avg_vol)

    # ── TENDÊNCIA (30 pts) ───────────────────────────────────────
    # ADX
    try:
        adx_data  = adx_fn(highs, lows, closes)
        adx_v     = adx_data["adx"]
        adx_trend = adx_v > 25
        adx_dir   = adx_data["direction"]
        adx_aligned = adx_dir == direction
    except Exception:
        adx_v = 20; adx_trend = False; adx_aligned = True

    # EMA stack
    e20  = float(ema(closes, 20)[-1])
    e50  = float(ema(closes, 50)[-1])
    e200 = float(ema(closes, min(200, len(closes)-1))[-1])
    bull = not np.isnan(e20) and not np.isnan(e50) and e20 > e50 and price > e20
    bear = not np.isnan(e20) and not np.isnan(e50) and e20 < e50 and price < e20
    full_stack = (bull and e50 > e200) or (bear and e50 < e200)
    aligned    = (direction == "LONG" and bull) or (direction == "SHORT" and bear)

    # SMC
    try:
        smc = smc_analysis(highs, lows, closes)
    except Exception:
        smc = {"structure":"UNKNOWN","hh":False,"hl":False,"lh":False,"ll":False,
               "bos":False,"bos_dir":"NONE","choch":False}

    # VWAP
    try:
        vwap_v = vwap_fn(highs, lows, closes, volumes)
        vwap_ok= (direction == "LONG" and price > vwap_v) or \
                 (direction == "SHORT" and price < vwap_v)
    except Exception:
        vwap_v = price; vwap_ok = True

    trend_s = 0
    # ADX contribution
    if adx_v > 30 and aligned:     trend_s += 10
    elif adx_v > 25 and aligned:   trend_s += 7
    elif adx_v > 20 and aligned:   trend_s += 4
    # EMA stack
    if full_stack and aligned:     trend_s += 10
    elif aligned:                  trend_s += 6
    # SMC BOS
    if smc["bos"] and direction in smc["bos_dir"]: trend_s += 6
    if (direction=="LONG" and smc["hh"] and smc["hl"]) or \
       (direction=="SHORT" and smc["lh"] and smc["ll"]): trend_s += 4
    # VWAP
    if vwap_ok: trend_s += 3
    # Penaliza ADX lateral e CHoCH
    if adx_v < 20:    trend_s = min(trend_s, 10)
    if smc["choch"]:  trend_s = max(0, trend_s - 5)
    trend_s = max(0, min(30, trend_s))

    # ── VOLUME (20 pts) ──────────────────────────────────────────
    try:
        ob = orderbook_imbalance(orderbook) if orderbook else {"bias":"NEUTRAL"}
        ob_ok = (direction=="LONG" and ob["bias"]=="BID_HEAVY") or \
                (direction=="SHORT" and ob["bias"]=="ASK_HEAVY")
    except Exception:
        ob_ok = False

    try:
        vp    = volume_profile(highs, lows, volumes)
        poc_ok= (direction=="LONG" and price > vp["poc"]) or \
                (direction=="SHORT" and price < vp["poc"])
    except Exception:
        poc_ok = True

    bodies_ok = [(closes[i] > opens[i]) == (direction=="LONG") for i in range(-3, 0)]
    if vol_r >= 1.8 and all(bodies_ok):         vol_s = 20
    elif vol_r >= 1.4 and sum(bodies_ok) >= 2:  vol_s = 14
    elif vol_r >= 1.1:                           vol_s = 9
    elif vol_r >= 0.8:                           vol_s = 5
    else:                                        vol_s = 2
    if ob_ok:  vol_s = min(20, vol_s + 2)
    if poc_ok: vol_s = min(20, vol_s + 2)
    vol_s = max(0, vol_s)

    # ── MOMENTUM (20 pts) ────────────────────────────────────────
    rsi_v = float(rsi(closes)[-1])
    try:
        _, _, hist = macd(closes)
        h0 = float(hist[-1]) if not np.isnan(hist[-1]) else 0
        h1 = float(hist[-2]) if len(hist)>1 and not np.isnan(hist[-2]) else h0
    except Exception:
        h0 = 0; h1 = 0

    try:
        fp = delta_footprint(closes, list(vols), opens)
        fp_ok = (direction=="LONG" and fp["bias"]=="BULLISH") or \
                (direction=="SHORT" and fp["bias"]=="BEARISH")
        fp_div = fp["divergence"]
    except Exception:
        fp_ok = True; fp_div = False

    if direction == "LONG":
        if 40 <= rsi_v <= 72:   rsi_s = 10
        elif 33 <= rsi_v < 40:  rsi_s = 6
        elif 72 < rsi_v <= 80:  rsi_s = 3
        else:                   rsi_s = 0
        if h0 > 0 and h0 > h1: macd_s = 10
        elif h0 > 0:            macd_s = 6
        elif h0 > h1:           macd_s = 3
        else:                   macd_s = 0
    else:
        if 28 <= rsi_v <= 60:   rsi_s = 10
        elif 60 < rsi_v <= 67:  rsi_s = 6
        elif 20 <= rsi_v < 28:  rsi_s = 3
        else:                   rsi_s = 0
        if h0 < 0 and h0 < h1: macd_s = 10
        elif h0 < 0:            macd_s = 6
        elif h0 < h1:           macd_s = 3
        else:                   macd_s = 0

    momentum_s = rsi_s + macd_s
    if fp_ok and not fp_div: momentum_s = min(20, momentum_s + 3)
    if fp_div:               momentum_s = max(0, momentum_s - 3)
    momentum_s = max(0, min(20, momentum_s))

    # ── VOLATILIDADE (15 pts) ────────────────────────────────────
    atr_pct       = atr_v / price * 100
    atr_expanding = atr_v > atr_avg * 1.03

    try:
        bb      = bollinger(closes)
        bb_squeeze = bb["squeezed"]
        bb_width   = bb["width"]
    except Exception:
        bb_squeeze = False; bb_width = 3.0

    try:
        ci_data  = chop_fn(highs, lows, closes)
        ci_chop  = ci_data["chop"]
        ci_trend = ci_data["trending"]
        ci_v     = ci_data["ci"]
    except Exception:
        ci_chop = False; ci_trend = True; ci_v = 50

    if atr_expanding and ci_trend and not bb_squeeze:   atr_s = 15
    elif atr_expanding and not ci_chop:                 atr_s = 11
    elif not ci_chop and 0.15 <= atr_pct <= 5.0:        atr_s = 8
    elif ci_chop:                                        atr_s = 3
    else:                                                atr_s = 5
    atr_s = max(0, min(15, atr_s))

    # ── ESTRUTURA (15 pts) ───────────────────────────────────────
    body     = abs(closes[-1] - opens[-1])
    cr       = highs[-1] - lows[-1]
    wick_r   = 1 - (body / cr) if cr > 0 else 1
    struct_s = 15
    if wick_r > 0.72:                             struct_s -= 5
    if vol_r > 3.0 and body < atr_v * 0.10:      struct_s -= 5
    if smc["choch"]:                              struct_s -= 4
    ph = max(highs[-6:-1]) if len(highs) > 6 else highs[-1]
    pl = min(lows[-6:-1])  if len(lows)  > 6 else lows[-1]
    if direction=="LONG"  and highs[-1]>ph and closes[-1]<ph: struct_s -= 5
    if direction=="SHORT" and lows[-1]<pl  and closes[-1]>pl: struct_s -= 5
    if ci_chop: struct_s = max(0, struct_s - 3)
    struct_s = max(0, min(15, struct_s))

    total = trend_s + vol_s + momentum_s + atr_s + struct_s

    return {
        "ok": True, "total": total,
        "trend_s": trend_s, "vol_s": vol_s,
        "momentum_s": momentum_s, "atr_s": atr_s, "struct_s": struct_s,
        "rsi_v": rsi_v, "rsi_s": rsi_s, "macd_s": macd_s,
        "adx_v": adx_v, "adx_trending": adx_trend, "adx_ranging": adx_v < 20,
        "ci_chop": ci_chop, "ci_trend": ci_trend, "ci_v": ci_v,
        "bb_squeeze": bb_squeeze, "bb_width": bb_width,
        "vwap": vwap_v, "vwap_ok": vwap_ok,
        "smc_structure": smc["structure"], "bos": smc["bos"],
        "choch": smc["choch"], "hh": smc["hh"], "hl": smc["hl"],
        "ob_bias": ob.get("bias","NEUTRAL") if orderbook else "N/A",
        "fp_ok": fp_ok, "fp_div": fp_div,
        "vol_r": vol_r, "atr_v": atr_v, "atr_expanding": atr_expanding,
        "aligned": aligned, "bull": bull, "bear": bear, "full": full_stack,
        "price": price,
        "summary": (
            f"T{trend_s}+V{vol_s}+M{momentum_s}+A{atr_s}+S{struct_s}={total} "
            f"ADX{adx_v:.0f} CI{ci_v:.0f} RSI{rsi_v:.0f}"
        ),
    }


# ─── Analyzer Principal ───────────────────────────────────────────
class Analyzer:
    def analyze_mtf(self, symbol, k15, k1h, k4h,
                    min_score=80, fee_mult=3.0, vol_mult=1.0) -> Optional[Signal]:
        if len(k4h) < 20 or len(k1h) < 20 or len(k15) < 30:
            return None

        def ga(kl):
            return ([k["c"] for k in kl], [k["h"] for k in kl],
                    [k["l"] for k in kl], [k["o"] for k in kl],
                    [k["v"] for k in kl])

        c4h,h4h,l4h,o4h,v4h = ga(k4h)
        c1h,h1h,l1h,o1h,v1h = ga(k1h)
        c15,h15,l15,o15,v15 = ga(k15)
        price = c15[-1]

        def get_atr(h, l, c):
            a = atr(h, l, c)
            return float(a[-1]), float(np.mean(a[-20:])) if len(a) >= 20 else float(a[-1])

        atr_4h, avg_4h = get_atr(h4h, l4h, c4h)
        atr_1h, avg_1h = get_atr(h1h, l1h, c1h)
        atr_15, avg_15 = get_atr(h15, l15, c15)

        # ── PASSO 1: Regime 4H ──────────────────────────────────
        regime = detect_regime(c4h, h4h, l4h, atr_4h)
        if regime == "COMPRESSED":
            log.debug(f"[{symbol}] 4H COMPRESSED → HOLD")
            return None

        # ── PASSO 2: Direção (4H + 1H) ─────────────────────────
        e20_4h = float(ema(c4h, 20)[-1])
        e50_4h = float(ema(c4h, 50)[-1])
        e20_1h = float(ema(c1h, 20)[-1])
        e50_1h = float(ema(c1h, 50)[-1])

        bull_4h = e20_4h > e50_4h and c4h[-1] > e20_4h
        bear_4h = e20_4h < e50_4h and c4h[-1] < e20_4h
        bull_1h = e20_1h > e50_1h and c1h[-1] > e20_1h
        bear_1h = e20_1h < e50_1h and c1h[-1] < e20_1h

        if bull_4h and bull_1h:
            direction = "LONG"
        elif bear_4h and bear_1h:
            direction = "SHORT"
        elif bull_1h and not bear_4h:
            direction = "LONG"
            min_score = max(min_score, min_score + 3)
        elif bear_1h and not bull_4h:
            direction = "SHORT"
            min_score = max(min_score, min_score + 3)
        else:
            log.debug(f"[{symbol}] 4H/1H conflito → HOLD")
            return None

        # ── PASSO 3: Score confluência ──────────────────────────
        s4h = score_tf(c4h, h4h, l4h, o4h, v4h, direction, atr_4h, avg_4h)
        s1h = score_tf(c1h, h1h, l1h, o1h, v1h, direction, atr_1h, avg_1h)
        s15 = score_tf(c15, h15, l15, o15, v15, direction, atr_15, avg_15)

        if not s4h["ok"] or not s1h["ok"] or not s15["ok"]:
            return None

        # Peso: 4H=25%, 1H=30%, 15M=45% (15M tem mais peso no timing)
        combined = round(s4h["total"]*0.25 + s1h["total"]*0.30 + s15["total"]*0.45)

        if combined < min_score:
            log.info(
                f"[{symbol}] Score={combined}/100 < {min_score} → HOLD "
                f"| 4H:{s4h['total']} 1H:{s1h['total']} 15M:{s15['total']} "
                f"| ADX={s15['adx_v']:.0f} CI={s15['ci_v']:.0f} "
                f"RSI={s15['rsi_v']:.0f} vol={s15['vol_r']:.2f}x "
                f"regime={regime}"
            )
            return None

        # ── PASSO 4: Bloqueios críticos ─────────────────────────
        # RSI extremo no 15M
        if s15["rsi_v"] > 82 or s15["rsi_v"] < 18:
            log.debug(f"[{symbol}] RSI extremo {s15['rsi_v']:.0f} → HOLD")
            return None
        # Volume muito fraco
        if s15["vol_r"] < 0.5:
            log.debug(f"[{symbol}] Volume fraco {s15['vol_r']:.2f}x → HOLD")
            return None
        # 15M não alinhado
        if not s15["aligned"]:
            log.debug(f"[{symbol}] 15M não alinhado → HOLD")
            return None

        # ── PASSO 5: Tipo de entrada ────────────────────────────
        entry_ok, entry_type = detect_entry(
            c15, h15, l15, o15, v15, direction, atr_15
        )
        if not entry_ok:
            combined = max(0, combined - 5)
            if combined < min_score:
                log.debug(f"[{symbol}] Sem setup de entrada → HOLD")
                return None

        # ── PASSO 6: SL/TP adaptativo por tipo de entrada ───────
        if entry_type == "BOS_BREAK":
            sl_mult, tp_mult = 1.2, 3.6   # R:R 1:3 — entrada mais cedo
        elif entry_type == "MOMENTUM":
            sl_mult, tp_mult = 1.5, 3.0   # R:R 1:2
        else:
            sl_mult, tp_mult = 2.0, 4.0   # R:R 1:2 — pullback clássico

        if direction == "LONG":
            sl = round(price - atr_1h * sl_mult, 6)
            tp = round(price + atr_1h * tp_mult, 6)
        else:
            sl = round(price + atr_1h * sl_mult, 6)
            tp = round(price - atr_1h * tp_mult, 6)

        rr = abs(tp - price) / abs(sl - price) if abs(sl - price) > 0 else 0
        if rr < 1.5:
            log.debug(f"[{symbol}] R:R {rr:.2f} < 1.5 → HOLD")
            return None

        # ── PASSO 7: Validação de taxas ─────────────────────────
        cost_pct   = TOTAL_COST * 100
        move_to_tp = abs(tp - price) / price * 100
        min_move   = cost_pct * fee_mult
        if move_to_tp < min_move:
            log.debug(f"[{symbol}] Move {move_to_tp:.3f}% < {min_move:.3f}% → HOLD")
            return None
        expected_net = move_to_tp - cost_pct

        # ── Monta sinal ─────────────────────────────────────────
        reasons = [
            f"4H:{s4h['total']}",
            f"1H:{s1h['total']}",
            f"15M:{s15['total']}",
            f"ADX{s15['adx_v']:.0f}",
            f"RR{rr:.1f}",
            f"ENTRY:{entry_type}",
        ]
        if s15["bos"]:      reasons.append("BOS✓")
        if s15["vwap_ok"]:  reasons.append("VWAP✓")
        if not s15["ci_chop"]: reasons.append("CI✓")
        if s15["choch"]:    reasons.append("CHoCH⚠")
        if s15["bb_squeeze"]: reasons.append("BB-SQZ")

        log.info(
            f"[{symbol}] ✅ SINAL {direction} score={combined}/100 "
            f"RR={rr:.1f} entry={entry_type} "
            f"| {' '.join(reasons)}"
        )

        return Signal(
            symbol=symbol, direction=direction,
            entry=price, sl=sl, tp=tp,
            confidence=min(0.97, combined/100),
            reason=" | ".join(reasons),
            score=int(combined),
            tf_4h=s4h["summary"], tf_1h=s1h["summary"], tf_15m=s15["summary"],
            expected_pnl=round(expected_net, 3),
            total_fees=round(cost_pct, 4),
            entry_type=entry_type,
        )

    def analyze(self, symbol, klines):
        return self.analyze_mtf(symbol, klines, klines, klines)

    def rank_signals(self, signals):
        return sorted([s for s in signals if s],
                      key=lambda s: s.score * s.rr, reverse=True)
