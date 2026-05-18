"""
KAKAZITO TRADE Strategy v10.0 — Quantitative Premium System
═══════════════════════════════════════════════════════════
Multi-Timeframe: 4H (tendência) → 1H (confirmação) → 15M (entrada)

REGRAS ABSOLUTAS:
  ✅ Score >= 90/100
  ✅ Volume > 1.5x média no 15M
  ✅ Lucro esperado >= 4x custo operacional
  ✅ R:R >= 1:2 (preferência 1:3)
  ✅ 4H, 1H e 15M devem alinhar
  ✅ ATR em expansão (mercado com força)
  ✅ Sem chop / lateralização
  ✅ Pullback confirmado antes da entrada

Score (100 pts):
  Tendência   = 30 pts  (4H + 1H EMA stack + HH/LL)
  Volume      = 20 pts  (fluxo institucional)
  Momentum    = 20 pts  (MACD + RSI zona ideal)
  Volatilidade= 15 pts  (ATR expansão, não comprimido)
  Estrutura   = 15 pts  (sem fake, sem chop)
"""
import numpy as np
from dataclasses import dataclass, field
from typing import Optional
from bot.indicators import ema, rsi, atr, macd
from bot.logger import log

# ── Custos operacionais Bybit ────────────────────────────────────
TAKER_FEE     = 0.00055   # 0.055% por execução
SLIPPAGE      = 0.00020   # 0.020% slippage estimado
FUNDING_FEE   = 0.00010   # 0.010% funding estimado (conservador)
TOTAL_COST    = (TAKER_FEE + SLIPPAGE) * 2 + FUNDING_FEE  # ~0.16% total


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
    rr:           float = field(init=False)

    def __post_init__(self):
        risk   = abs(self.entry - self.sl)
        reward = abs(self.tp - self.entry)
        self.rr = round(reward / risk, 2) if risk > 0 else 0


# ─────────────────────────────────────────────────────────────────
# DETECTOR DE REGIME DE MERCADO
# ─────────────────────────────────────────────────────────────────
def detect_regime(closes, highs, lows, atr_v) -> str:
    """
    Classifica o regime atual:
      TRENDING_UP | TRENDING_DOWN | RANGING | COMPRESSED | REVERSAL
    """
    if len(closes) < 30:
        return "UNKNOWN"

    price   = closes[-1]
    e20     = ema(closes, 20)
    e50     = ema(closes, 50)
    atr_arr = atr(highs, lows, closes)

    # ATR médio das últimas 20 velas vs atual
    atr_avg = float(np.mean(atr_arr[-20:])) if len(atr_arr) >= 20 else atr_v
    atr_pct = atr_v / price * 100

    # Range das últimas 20 velas
    recent_high = max(highs[-20:])
    recent_low  = min(lows[-20:])
    range_pct   = (recent_high - recent_low) / price * 100

    # Cruzamentos EMA20 (chop indicator)
    crossings = sum(
        1 for i in range(-9, -1)
        if (closes[i] > e20[i]) != (closes[i-1] > e20[i-1])
    )

    bull_stack = (not np.isnan(e20[-1])) and (not np.isnan(e50[-1])) and e20[-1] > e50[-1] and price > e20[-1]
    bear_stack = (not np.isnan(e20[-1])) and (not np.isnan(e50[-1])) and e20[-1] < e50[-1] and price < e20[-1]

    # Comprimido: ATR < 70% da média histórica
    if atr_v < atr_avg * 0.70:
        return "COMPRESSED"

    # Chop: muitos cruzamentos e range estreito
    if crossings >= 4 and range_pct < atr_pct * 2.5:
        return "RANGING"

    # Tendência
    if bull_stack and atr_v >= atr_avg * 0.90:
        return "TRENDING_UP"
    if bear_stack and atr_v >= atr_avg * 0.90:
        return "TRENDING_DOWN"

    return "RANGING"


# ─────────────────────────────────────────────────────────────────
# DETECTOR DE PULLBACK
# ─────────────────────────────────────────────────────────────────
def detect_pullback(closes, highs, lows, direction, atr_v) -> bool:
    """
    Confirma que houve um pullback saudável antes de entrar.
    LONG: preço recuou ao menos 1x ATR após alta e voltou.
    SHORT: preço subiu ao menos 1x ATR após queda e voltou.
    """
    if len(closes) < 10:
        return False
    price = closes[-1]

    if direction == "LONG":
        # Procura mínimo local nos últimos 8 candles
        local_min = min(lows[-8:])
        # Pullback válido: preço caiu ao menos 1x ATR e recuperou
        return (max(highs[-8:]) - local_min) >= atr_v * 0.8 and price > local_min + atr_v * 0.3
    else:
        local_max = max(highs[-8:])
        return (local_max - min(lows[-8:])) >= atr_v * 0.8 and price < local_max - atr_v * 0.3


# ─────────────────────────────────────────────────────────────────
# SCORE DE CONFLUÊNCIA
# ─────────────────────────────────────────────────────────────────
def score_tf(closes, highs, lows, opens, volumes, direction, atr_v, atr_avg) -> dict:
    """Calcula score 0-100 para um único timeframe."""
    price = closes[-1]
    if price <= 0 or atr_v <= 0:
        return {"ok": False, "total": 0}

    vols    = np.array(volumes, dtype=float)
    avg_vol = vols[-21:-1].mean() if len(vols) > 21 else (vols.mean() or 1)
    vol_r   = vols[-1] / avg_vol

    # ── TENDÊNCIA (30 pts) ──────────────────────────────────────
    e20  = ema(closes, 20)[-1]
    e50  = ema(closes, 50)[-1]
    e200 = ema(closes, min(200, len(closes)-1))[-1]

    bull = not np.isnan(e20) and not np.isnan(e50) and e20 > e50 and price > e20
    bear = not np.isnan(e20) and not np.isnan(e50) and e20 < e50 and price < e20
    full = (bull and not np.isnan(e200) and e50 > e200) or (bear and not np.isnan(e200) and e50 < e200)
    aligned = (direction == "LONG" and bull) or (direction == "SHORT" and bear)

    # Higher Highs / Lower Lows
    rh = [max(highs[i-3:i+3]) for i in range(5, len(highs)-3, 5)][-3:]
    rl = [min(lows[i-3:i+3])  for i in range(5, len(lows)-3,  5)][-3:]
    if direction == "LONG":
        hh_ll = len(rh) >= 2 and (rh[-1] > rh[-2]) and (rl[-1] > rl[-2])
    else:
        hh_ll = len(rh) >= 2 and (rh[-1] < rh[-2]) and (rl[-1] < rl[-2])

    if full and aligned and hh_ll: trend_s = 30
    elif full and aligned:          trend_s = 22
    elif aligned and hh_ll:         trend_s = 16
    elif aligned:                   trend_s = 10
    else:                           trend_s = 0

    # ── VOLUME (20 pts) ─────────────────────────────────────────
    bodies_ok = [(closes[i] > opens[i]) == (direction == "LONG") for i in range(-3, 0)]
    if vol_r >= 2.5 and all(bodies_ok):   vol_s = 20
    elif vol_r >= 2.0 and sum(bodies_ok) >= 2: vol_s = 16
    elif vol_r >= 1.5 and sum(bodies_ok) >= 1: vol_s = 11
    elif vol_r >= 1.2:                    vol_s = 6
    else:                                 vol_s = 0   # abaixo da média → sem edge

    # ── MOMENTUM (20 pts) ───────────────────────────────────────
    rsi_v = rsi(closes)[-1]
    _, _, hist = macd(closes)
    h0 = hist[-1] if not np.isnan(hist[-1]) else 0
    h1 = hist[-2] if len(hist) > 1 and not np.isnan(hist[-2]) else h0

    if direction == "LONG":
        if 45 <= rsi_v <= 65:  rsi_s = 10
        elif 38 <= rsi_v < 45: rsi_s = 6
        elif 65 < rsi_v <= 72: rsi_s = 3
        else:                  rsi_s = 0
        if h0 > 0 and h0 > h1: macd_s = 10
        elif h0 > 0:            macd_s = 6
        elif h0 > h1:           macd_s = 3
        else:                   macd_s = 0
    else:
        if 35 <= rsi_v <= 55:  rsi_s = 10
        elif 55 < rsi_v <= 62: rsi_s = 6
        elif 28 <= rsi_v < 35: rsi_s = 3
        else:                  rsi_s = 0
        if h0 < 0 and h0 < h1: macd_s = 10
        elif h0 < 0:            macd_s = 6
        elif h0 < h1:           macd_s = 3
        else:                   macd_s = 0

    momentum_s = rsi_s + macd_s

    # ── VOLATILIDADE (15 pts) ───────────────────────────────────
    atr_pct      = atr_v / price * 100
    atr_expanding = atr_v > atr_avg * 1.05

    if atr_expanding and 0.3 <= atr_pct <= 5.0: atr_s = 15
    elif atr_expanding:                           atr_s = 9
    elif 0.3 <= atr_pct <= 3.0:                  atr_s = 6
    else:                                         atr_s = 0

    # ── ESTRUTURA (15 pts) ──────────────────────────────────────
    body  = abs(closes[-1] - opens[-1])
    cr    = highs[-1] - lows[-1]
    wick  = 1 - (body / cr) if cr > 0 else 1
    struct_s = 15
    if wick > 0.65:                            struct_s -= 8
    if vol_r > 3.0 and body < atr_v * 0.15:   struct_s -= 6
    ph = max(highs[-6:-1]) if len(highs) > 6 else highs[-1]
    pl = min(lows[-6:-1])  if len(lows)  > 6 else lows[-1]
    if direction == "LONG"  and highs[-1] > ph and closes[-1] < ph: struct_s -= 8
    if direction == "SHORT" and lows[-1]  < pl and closes[-1] > pl: struct_s -= 8
    struct_s = max(0, struct_s)

    total = trend_s + vol_s + momentum_s + atr_s + struct_s

    return {
        "ok": True, "total": total,
        "trend_s": trend_s, "vol_s": vol_s,
        "momentum_s": momentum_s, "atr_s": atr_s, "struct_s": struct_s,
        "rsi_v": rsi_v, "rsi_s": rsi_s, "macd_s": macd_s,
        "vol_r": vol_r, "atr_v": atr_v, "atr_expanding": atr_expanding,
        "aligned": aligned, "bull": bull, "bear": bear, "full": full,
        "price": price,
        "summary": f"T{trend_s}+V{vol_s}+M{momentum_s}+A{atr_s}+S{struct_s}={total}",
    }


# ─────────────────────────────────────────────────────────────────
# ANALYZER PRINCIPAL
# ─────────────────────────────────────────────────────────────────
class Analyzer:
    def analyze_mtf(
        self,
        symbol:    str,
        k15:       list,
        k1h:       list,
        k4h:       list,
        min_score: int = 90,
        fee_mult:  float = 4.0,
        vol_mult:  float = 1.5,
    ) -> Optional[Signal]:
        """
        PASSO 1: Regime do 4H → define se é seguro operar
        PASSO 2: Direção do 4H + 1H → define viés
        PASSO 3: Score confluência 3 TFs
        PASSO 4: Pullback confirmado no 15M
        PASSO 5: Volume institucional no 15M
        PASSO 6: Custo operacional validado
        PASSO 7: R:R validado
        PASSO 8: Emite sinal ou HOLD
        """
        if len(k4h) < 30 or len(k1h) < 30 or len(k15) < 60:
            return None

        def arr(kl):
            return ([k["c"] for k in kl], [k["h"] for k in kl],
                    [k["l"] for k in kl], [k["o"] for k in kl],
                    [k["v"] for k in kl])

        c4h,h4h,l4h,o4h,v4h = arr(k4h)
        c1h,h1h,l1h,o1h,v1h = arr(k1h)
        c15,h15,l15,o15,v15 = arr(k15)

        price = c15[-1]

        # ── ATR de cada TF ──────────────────────────────────────
        def get_atr(h, l, c):
            a = atr(h, l, c)
            v = a[-1]
            avg = float(np.mean(a[-20:])) if len(a) >= 20 else v
            return v, avg

        atr_4h, avg_4h = get_atr(h4h, l4h, c4h)
        atr_1h, avg_1h = get_atr(h1h, l1h, c1h)
        atr_15, avg_15 = get_atr(h15, l15, c15)

        # ── PASSO 1: Regime do 4H ───────────────────────────────
        regime = detect_regime(c4h, h4h, l4h, atr_4h)
        if regime in ("COMPRESSED", "RANGING"):
            log.debug(f"[{symbol}] 4H regime={regime} → HOLD")
            return None

        # ── PASSO 2: Direção (4H + 1H devem concordar) ─────────
        e20_4h = ema(c4h, 20)[-1]
        e50_4h = ema(c4h, 50)[-1]
        e20_1h = ema(c1h, 20)[-1]
        e50_1h = ema(c1h, 50)[-1]

        bull_4h = not np.isnan(e20_4h) and e20_4h > e50_4h and c4h[-1] > e20_4h
        bear_4h = not np.isnan(e20_4h) and e20_4h < e50_4h and c4h[-1] < e20_4h
        bull_1h = not np.isnan(e20_1h) and e20_1h > e50_1h and c1h[-1] > e20_1h
        bear_1h = not np.isnan(e20_1h) and e20_1h < e50_1h and c1h[-1] < e20_1h

        if bull_4h and bull_1h:
            direction = "LONG"
        elif bear_4h and bear_1h:
            direction = "SHORT"
        else:
            log.debug(f"[{symbol}] 4H/1H conflito: 4H={'bull' if bull_4h else 'bear'} 1H={'bull' if bull_1h else 'bear'} → HOLD")
            return None

        # ── PASSO 3: Regime do 15M ──────────────────────────────
        regime_15 = detect_regime(c15, h15, l15, atr_15)
        if regime_15 == "COMPRESSED":
            log.debug(f"[{symbol}] 15M comprimido → HOLD")
            return None

        # ── PASSO 4: Score de confluência ───────────────────────
        s4h = score_tf(c4h, h4h, l4h, o4h, v4h, direction, atr_4h, avg_4h)
        s1h = score_tf(c1h, h1h, l1h, o1h, v1h, direction, atr_1h, avg_1h)
        s15 = score_tf(c15, h15, l15, o15, v15, direction, atr_15, avg_15)

        if not s4h["ok"] or not s1h["ok"] or not s15["ok"]:
            return None

        # Peso: 4H=30%, 1H=30%, 15M=40%
        combined = round(s4h["total"]*0.30 + s1h["total"]*0.30 + s15["total"]*0.40)

        if combined < min_score:
            log.debug(f"[{symbol}] Score {combined}/100 < {min_score} → HOLD | 4H:{s4h['total']} 1H:{s1h['total']} 15M:{s15['total']}")
            return None

        # ── PASSO 5: Bloqueios críticos ─────────────────────────
        # RSI extremo no 15M (timing)
        if s15["rsi_s"] == 0:
            log.debug(f"[{symbol}] RSI 15M extremo ({s15['rsi_v']:.0f}) → HOLD")
            return None

        # Volume mínimo obrigatório no 15M
        if s15["vol_r"] < vol_mult:
            log.debug(f"[{symbol}] Volume 15M {s15['vol_r']:.2f}x < {vol_mult}x → HOLD")
            return None

        # Direção não alinhada no 15M
        if not s15["aligned"]:
            log.debug(f"[{symbol}] 15M EMA não alinha com {direction} → HOLD")
            return None

        # ATR comprimido nos 3 TFs → sem força
        if not s4h["atr_expanding"] and not s1h["atr_expanding"] and not s15["atr_expanding"]:
            log.debug(f"[{symbol}] ATR comprimido em todos TFs → HOLD")
            return None

        # ── PASSO 6: Pullback no 15M ────────────────────────────
        if not detect_pullback(c15, h15, l15, direction, atr_15):
            log.debug(f"[{symbol}] Sem pullback confirmado no 15M → aguardando")
            return None

        # ── PASSO 7: SL e TP adaptativos (ATR do 1H) ────────────
        # SL = 2x ATR 1H (estável, evita stop hunt)
        # TP = 4x ATR 1H (R:R 1:2)
        sl_dist = atr_1h * 2.0
        tp_dist = atr_1h * 4.0

        if direction == "LONG":
            sl = round(price - sl_dist, 6)
            tp = round(price + tp_dist, 6)
        else:
            sl = round(price + sl_dist, 6)
            tp = round(price - tp_dist, 6)

        rr = tp_dist / sl_dist if sl_dist > 0 else 0
        if rr < 1.8:
            log.debug(f"[{symbol}] R:R {rr:.2f} < 1.8 → HOLD")
            return None

        # ── PASSO 8: Validação de custo operacional ─────────────
        cost_pct      = TOTAL_COST * 100
        min_move      = cost_pct * fee_mult
        move_to_tp    = tp_dist / price * 100

        if move_to_tp < min_move:
            log.debug(f"[{symbol}] Move {move_to_tp:.3f}% < mínimo {min_move:.3f}% → HOLD (taxas não cobertas)")
            return None

        expected_net = move_to_tp - cost_pct

        # ── Monta sinal ─────────────────────────────────────────
        reasons = []
        if s4h["total"] >= 70: reasons.append(f"4H✓({s4h['total']})")
        if s1h["total"] >= 70: reasons.append(f"1H✓({s1h['total']})")
        if s15["total"] >= 70: reasons.append(f"15M✓({s15['total']})")
        if s15["vol_r"] >= 2:  reasons.append(f"VOL{s15['vol_r']:.1f}x")
        reasons.append(f"RR{rr:.1f}")
        reasons.append(f"regime={regime}")

        log.info(
            f"[{symbol}] ✅ SINAL PREMIUM {direction} | Score={combined}/100 | "
            f"R:R={rr:.1f} | Move={move_to_tp:.2f}% | Líq≈+{expected_net:.2f}% | "
            f"4H:[{s4h['summary']}] 1H:[{s1h['summary']}] 15M:[{s15['summary']}] | "
            f"Regime={regime}"
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
        )

    def analyze(self, symbol, klines):
        """Fallback single-TF."""
        return self.analyze_mtf(symbol, klines, klines, klines)

    def rank_signals(self, signals):
        """Score × R:R como critério de ranking."""
        return sorted([s for s in signals if s], key=lambda s: s.score * s.rr, reverse=True)
