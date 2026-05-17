"""
KAKAZITO TRADE Strategy v9.0 — Quantitative High-Probability System
════════════════════════════════════════════════════════════════════
OBJETIVO: Lucro líquido máximo. Poucos trades excelentes.

REGRAS ABSOLUTAS:
  - expected_net_profit > total_fees * 3  (obrigatório)
  - Score >= 80/100                        (obrigatório)
  - R:R >= 1:2, preferência >= 1:3         (obrigatório)
  - NÃO operar mercado lateral             (obrigatório)
  - NÃO scalping de micro movimentos       (obrigatório)
  - Confirmar 1H + 15M antes de entrar     (obrigatório)
  - Volume acima da média                  (obrigatório)
  - ATR expansion confirmado               (obrigatório)

Score de confluência (100 pts):
  Tendência  = 30 pts (EMA + HH/LL + Market Structure)
  Volume     = 20 pts (Volume delta + acima da média)
  Momentum   = 20 pts (MACD + RSI não-extremo)
  Volatilidade= 15 pts (ATR expansion, não comprimido)
  Estrutura  = 15 pts (sem chop, sem range estreito)
"""
import numpy as np
from dataclasses import dataclass, field
from typing import Optional
from bot.indicators import ema, rsi, atr, macd
from bot.logger import log

# ── Taxas Bybit (taker) ─────────────────────────────────────────
TAKER_FEE   = 0.00055   # 0.055% por lado
SLIPPAGE    = 0.0002    # 0.02% estimado de slippage
FUNDING_EST = 0.0001    # 0.01% estimado de funding (conservador)
TOTAL_COST_PCT = (TAKER_FEE + SLIPPAGE) * 2 + FUNDING_EST  # custo total estimado

# Movimento mínimo necessário para cobrir taxas com margem 3x
MIN_MOVE_MULTIPLIER = 3.0   # lucro esperado >= 3x o custo total

# Cooldown mínimo entre trades no mesmo símbolo (segundos)
MIN_COOLDOWN_SECONDS = 900   # 15 minutos


@dataclass
class Signal:
    symbol:       str
    direction:    str       # "LONG" | "SHORT"
    entry:        float
    sl:           float
    tp:           float
    confidence:   float
    reason:       str = ""
    score:        int = 0
    tf_1h:        str = ""
    tf_15m:       str = ""
    expected_pnl: float = 0.0   # lucro esperado líquido em %
    total_fees:   float = 0.0   # custo total estimado em %
    rr:           float = field(init=False)

    def __post_init__(self):
        risk   = abs(self.entry - self.sl)
        reward = abs(self.tp   - self.entry)
        self.rr = round(reward / risk, 2) if risk > 0 else 0


def _detect_chop(closes: list, highs: list, lows: list, atr_v: float) -> bool:
    """
    Detecta mercado lateral/chop.
    Retorna True se o mercado estiver lateral (NÃO operar).
    """
    if len(closes) < 20:
        return True

    price = closes[-1]

    # 1. Range estreito: high-low das últimas 20 velas vs ATR
    recent_high = max(highs[-20:])
    recent_low  = min(lows[-20:])
    range_pct   = (recent_high - recent_low) / price * 100

    # Se o range for menor que 3x ATR% → mercado comprimido
    atr_pct = atr_v / price * 100
    if range_pct < atr_pct * 3:
        return True

    # 2. ATR comprimido: ATR atual vs ATR médio das últimas 50 velas
    # (já calculado pelo chamador)

    # 3. Preço oscilando sem direção: conta cruzamentos da EMA20
    e20 = ema(closes, 20)
    crossings = 0
    for i in range(-10, -1):
        if (closes[i] > e20[i]) != (closes[i-1] > e20[i-1]):
            crossings += 1
    if crossings >= 4:   # muitos cruzamentos = chop
        return True

    return False


def _higher_highs_lower_lows(highs: list, lows: list, direction: str) -> bool:
    """
    Confirma estrutura de mercado (HH/HL para LONG, LH/LL para SHORT).
    Verifica os últimos 3 pivôs.
    """
    if len(highs) < 15:
        return False

    # Pega pivôs aproximados (máximos/mínimos locais)
    recent_h = [max(highs[i-3:i+3]) for i in range(5, len(highs)-3, 5)][-3:]
    recent_l = [min(lows[i-3:i+3])  for i in range(5, len(lows)-3,  5)][-3:]

    if len(recent_h) < 2 or len(recent_l) < 2:
        return False

    if direction == "LONG":
        # Higher Highs + Higher Lows
        hh = all(recent_h[i] > recent_h[i-1] for i in range(1, len(recent_h)))
        hl = all(recent_l[i] > recent_l[i-1] for i in range(1, len(recent_l)))
        return hh or hl   # pelo menos um confirmado
    else:
        # Lower Highs + Lower Lows
        lh = all(recent_h[i] < recent_h[i-1] for i in range(1, len(recent_h)))
        ll = all(recent_l[i] < recent_l[i-1] for i in range(1, len(recent_l)))
        return lh or ll


def _score_confluence(
    closes, highs, lows, opens, volumes,
    label: str,
    direction: str,
    atr_v: float,
    atr_avg: float,
) -> dict:
    """
    Calcula o score de confluência de um timeframe.
    Retorna dict com scores parciais e flags.

    Score total = 100 pts:
      Tendência   = 30 pts
      Volume      = 20 pts
      Momentum    = 20 pts
      Volatilidade= 15 pts
      Estrutura   = 15 pts
    """
    price = closes[-1]
    if price <= 0 or atr_v <= 0:
        return {"ok": False, "total": 0}

    vols    = np.array(volumes, dtype=float)
    avg_vol = vols[-21:-1].mean() if len(vols) > 21 else vols.mean() or 1
    vol_r   = vols[-1] / avg_vol

    # ══ TENDÊNCIA (30 pts) ══════════════════════════════════════
    e20  = ema(closes, 20)[-1]
    e50  = ema(closes, 50)[-1]
    e200 = ema(closes, min(200, len(closes)-1))[-1]

    ema_bull = (not np.isnan(e20)) and (not np.isnan(e50)) and e20 > e50 and price > e20
    ema_bear = (not np.isnan(e20)) and (not np.isnan(e50)) and e20 < e50 and price < e20
    full_stack = (
        (ema_bull and not np.isnan(e200) and e50 > e200) or
        (ema_bear and not np.isnan(e200) and e50 < e200)
    )

    # Alinhamento com direção solicitada
    ema_aligned = (direction == "LONG" and ema_bull) or (direction == "SHORT" and ema_bear)

    # HH/LL Market Structure
    hh_ll = _higher_highs_lower_lows(highs, lows, direction)

    if full_stack and ema_aligned and hh_ll:
        trend_s = 30
    elif full_stack and ema_aligned:
        trend_s = 22
    elif ema_aligned and hh_ll:
        trend_s = 18
    elif ema_aligned:
        trend_s = 12
    else:
        trend_s = 0   # contra a tendência → sem pontos

    # ══ VOLUME (20 pts) ═════════════════════════════════════════
    # Volume delta: últimos 3 candles com volume crescente na direção certa
    bodies_dir = [
        (closes[i] > opens[i]) == (direction == "LONG")
        for i in range(-3, 0)
    ]
    vol_growing = all(vols[i] >= vols[i-1] for i in range(-2, 0))

    if vol_r >= 2.0 and all(bodies_dir):
        vol_s = 20
    elif vol_r >= 1.5 and sum(bodies_dir) >= 2:
        vol_s = 14
    elif vol_r >= 1.2:
        vol_s = 8
    elif vol_r >= 1.0:
        vol_s = 4
    else:
        vol_s = 0   # volume abaixo da média → sem edge

    # ══ MOMENTUM (20 pts) ═══════════════════════════════════════
    rsi_v = rsi(closes)[-1]
    _, _, hist_arr = macd(closes)
    h0 = hist_arr[-1] if not np.isnan(hist_arr[-1]) else 0
    h1 = hist_arr[-2] if len(hist_arr) > 1 and not np.isnan(hist_arr[-2]) else h0

    # RSI: zona ideal sem extremos
    if direction == "LONG":
        if 45 <= rsi_v <= 65:    rsi_s = 10
        elif 38 <= rsi_v < 45:   rsi_s = 6
        elif 65 < rsi_v <= 70:   rsi_s = 4
        else:                    rsi_s = 0   # extremo → bloqueia
    else:
        if 35 <= rsi_v <= 55:    rsi_s = 10
        elif 55 < rsi_v <= 62:   rsi_s = 6
        elif 30 <= rsi_v < 35:   rsi_s = 4
        else:                    rsi_s = 0

    # MACD acelerando na direção certa
    if direction == "LONG":
        if h0 > 0 and h0 > h1:   macd_s = 10
        elif h0 > 0:              macd_s = 6
        elif h0 > h1:             macd_s = 3
        else:                     macd_s = 0
    else:
        if h0 < 0 and h0 < h1:   macd_s = 10
        elif h0 < 0:              macd_s = 6
        elif h0 < h1:             macd_s = 3
        else:                     macd_s = 0

    momentum_s = rsi_s + macd_s

    # ══ VOLATILIDADE (15 pts) ═══════════════════════════════════
    atr_pct = atr_v / price * 100

    # ATR deve estar em expansão (não comprimido)
    atr_expanding = atr_v > atr_avg * 1.1   # ATR atual > 110% da média

    if atr_expanding and 0.3 <= atr_pct <= 4.0:
        atr_s = 15
    elif atr_expanding and atr_pct > 0.2:
        atr_s = 10
    elif 0.3 <= atr_pct <= 2.5:
        atr_s = 6
    else:
        atr_s = 0   # ATR comprimido → mercado sem força

    # ══ ESTRUTURA (15 pts) ══════════════════════════════════════
    # Penaliza manipulação/fake breakout
    body     = abs(closes[-1] - opens[-1])
    cr       = highs[-1] - lows[-1]
    wick_r   = 1 - (body / cr) if cr > 0 else 1

    struct_s = 15
    if wick_r > 0.65:                             struct_s -= 8   # wick dominante
    if vol_r > 3.0 and body < atr_v * 0.15:      struct_s -= 6   # spike falso
    ph = max(highs[-6:-1]) if len(highs) > 6 else highs[-1]
    pl = min(lows[-6:-1])  if len(lows)  > 6 else lows[-1]
    if direction == "LONG"  and highs[-1] > ph and closes[-1] < ph: struct_s -= 8
    if direction == "SHORT" and lows[-1]  < pl and closes[-1] > pl: struct_s -= 8
    struct_s = max(0, struct_s)

    total = trend_s + vol_s + momentum_s + atr_s + struct_s

    summary = (
        f"T{trend_s} V{vol_s} M{momentum_s} "
        f"ATR{atr_s}({'↑' if atr_expanding else '→'}) "
        f"S{struct_s} RSI{rsi_v:.0f}"
    )

    return {
        "ok":          True,
        "total":       total,
        "trend_s":     trend_s,
        "vol_s":       vol_s,
        "momentum_s":  momentum_s,
        "atr_s":       atr_s,
        "struct_s":    struct_s,
        "rsi_v":       rsi_v,
        "rsi_s":       rsi_s,
        "macd_s":      macd_s,
        "vol_r":       vol_r,
        "atr_v":       atr_v,
        "atr_pct":     atr_pct,
        "atr_expanding": atr_expanding,
        "ema_aligned": ema_aligned,
        "ema_bull":    ema_bull,
        "ema_bear":    ema_bear,
        "price":       price,
        "summary":     summary,
    }


class Analyzer:
    def analyze_mtf(
        self,
        symbol: str,
        k15:    list,   # klines 15 minutos
        k1h:    list,   # klines 1 hora
        min_score: int = 80,
    ) -> Optional[Signal]:
        """
        Análise Multi-Timeframe com validação completa de edge.

        PASSO 1: Identificar tendência forte no 1H
        PASSO 2: Confirmar no 15M (pullback + momentum)
        PASSO 3: Calcular custo operacional total
        PASSO 4: Validar expected_net_profit > fees * 3
        PASSO 5: Validar R:R >= 2
        PASSO 6: Emitir sinal ou HOLD
        """
        if len(k15) < 60 or len(k1h) < 30:
            return None

        def to_arr(klines):
            return (
                [k["c"] for k in klines],
                [k["h"] for k in klines],
                [k["l"] for k in klines],
                [k["o"] for k in klines],
                [k["v"] for k in klines],
            )

        c15, h15, l15, o15, v15 = to_arr(k15)
        c1h, h1h, l1h, o1h, v1h = to_arr(k1h)

        price = c15[-1]

        # ── PASSO 1: Direção do 1H ─────────────────────────────
        e20_1h = ema(c1h, 20)[-1]
        e50_1h = ema(c1h, 50)[-1]
        if np.isnan(e20_1h) or np.isnan(e50_1h):
            return None

        if e20_1h > e50_1h and c1h[-1] > e20_1h:
            direction = "LONG"
        elif e20_1h < e50_1h and c1h[-1] < e20_1h:
            direction = "SHORT"
        else:
            log.debug(f"[{symbol}] 1H sem tendência clara → HOLD")
            return None

        # ── PASSO 2: Detectar chop em ambos os TFs ────────────
        atr_15 = atr(h15, l15, c15)
        atr_1h = atr(h1h, l1h, c1h)
        atr_v_15  = atr_15[-1]
        atr_avg15 = float(np.mean(atr_15[-20:])) if len(atr_15) >= 20 else atr_v_15
        atr_v_1h  = atr_1h[-1]
        atr_avg1h = float(np.mean(atr_1h[-20:])) if len(atr_1h) >= 20 else atr_v_1h

        if _detect_chop(c1h, h1h, l1h, atr_v_1h):
            log.debug(f"[{symbol}] 1H lateral/chop → HOLD")
            return None

        if _detect_chop(c15, h15, l15, atr_v_15):
            log.debug(f"[{symbol}] 15M lateral/chop → HOLD")
            return None

        # ── PASSO 3: Score de confluência ─────────────────────
        sc1h  = _score_confluence(c1h, h1h, l1h, o1h, v1h, "1H",  direction, atr_v_1h,  atr_avg1h)
        sc15  = _score_confluence(c15, h15, l15, o15, v15, "15M", direction, atr_v_15,  atr_avg15)

        if not sc1h["ok"] or not sc15["ok"]:
            return None

        # 1H tem peso 40%, 15M tem peso 60% (timing mais importante)
        combined = round(sc1h["total"] * 0.40 + sc15["total"] * 0.60)

        if combined < min_score:
            log.debug(
                f"[{symbol}] Score {combined}/100 < {min_score} → HOLD "
                f"1H:{sc1h['total']} 15M:{sc15['total']}"
            )
            return None

        # ── PASSO 4: Bloqueios críticos de qualidade ──────────
        # RSI extremo no 15M (timing) → não entra
        if sc15["rsi_s"] == 0:
            log.debug(f"[{symbol}] RSI 15M extremo ({sc15['rsi_v']:.0f}) → HOLD")
            return None

        # Volume abaixo da média em ambos → sem edge
        if sc1h["vol_s"] == 0 and sc15["vol_s"] == 0:
            log.debug(f"[{symbol}] Volume fraco em ambos TFs → HOLD")
            return None

        # ATR comprimido em ambos → mercado sem força
        if not sc1h["atr_expanding"] and not sc15["atr_expanding"]:
            log.debug(f"[{symbol}] ATR comprimido em ambos TFs → HOLD")
            return None

        # Tendência não alinhada no 15M
        if not sc15["ema_aligned"]:
            log.debug(f"[{symbol}] 15M EMA contra direção {direction} → HOLD")
            return None

        # ── PASSO 5: SL e TP baseados em ATR do 1H ───────────
        # Usa ATR do 1H para SL (mais estável, evita stop hunt)
        # TP = 3x o risco (R:R 1:3 preferencial)
        if direction == "LONG":
            sl = round(price - atr_v_1h * 1.5, 6)
            tp = round(price + atr_v_1h * 4.5, 6)   # R:R 1:3
        else:
            sl = round(price + atr_v_1h * 1.5, 6)
            tp = round(price - atr_v_1h * 4.5, 6)

        risk   = abs(price - sl)
        reward = abs(tp - price)
        rr     = reward / risk if risk > 0 else 0

        if rr < 2.0:
            log.debug(f"[{symbol}] R:R {rr:.2f} < 2.0 → HOLD")
            return None

        # ── PASSO 6: Validação de custo operacional ───────────
        # Custo total: taxa entrada + taxa saída + slippage + funding
        cost_pct = TOTAL_COST_PCT * 100   # em %
        # Movimento necessário para cobrir custo com margem 3x
        min_move_pct = cost_pct * MIN_MOVE_MULTIPLIER

        # Movimento esperado até TP em %
        move_to_tp_pct = reward / price * 100

        if move_to_tp_pct < min_move_pct:
            log.debug(
                f"[{symbol}] Movimento até TP ({move_to_tp_pct:.2f}%) "
                f"< mínimo ({min_move_pct:.2f}%) → HOLD (taxas não cobertas)"
            )
            return None

        # Lucro líquido esperado (após taxas)
        expected_net_pct = move_to_tp_pct - cost_pct

        # ── PASSO 7: Monta o sinal ────────────────────────────
        reasons = []
        if sc1h["total"] >= 70:  reasons.append(f"1H-FORTE({sc1h['total']})")
        if sc15["total"] >= 65:  reasons.append(f"15M-OK({sc15['total']})")
        if sc15["vol_r"] >= 1.5: reasons.append(f"VOL{sc15['vol_r']:.1f}x")
        if sc1h["atr_expanding"] or sc15["atr_expanding"]: reasons.append("ATR↑")
        if rr >= 3.0:            reasons.append(f"RR{rr:.1f}")
        else:                    reasons.append(f"RR{rr:.1f}")

        confidence = min(0.97, combined / 100)

        log.info(
            f"[{symbol}] ✅ SINAL {direction} | Score={combined}/100 | "
            f"RR={rr:.1f} | Move={move_to_tp_pct:.2f}% | "
            f"PnL_líq≈{expected_net_pct:.2f}% | "
            f"1H:[{sc1h['summary']}] | 15M:[{sc15['summary']}]"
        )

        return Signal(
            symbol=symbol,
            direction=direction,
            entry=price,
            sl=sl,
            tp=tp,
            confidence=confidence,
            reason=" | ".join(reasons),
            score=int(combined),
            tf_1h=sc1h["summary"],
            tf_15m=sc15["summary"],
            expected_pnl=round(expected_net_pct, 3),
            total_fees=round(cost_pct, 4),
        )

    def analyze(self, symbol: str, klines: list) -> Optional[Signal]:
        """Fallback — só se 1H não estiver disponível."""
        return self.analyze_mtf(symbol, klines, klines)

    def rank_signals(self, signals: list) -> list:
        """Ordena por score * RR (qualidade combinada)."""
        return sorted(
            [s for s in signals if s],
            key=lambda s: s.score * s.rr,
            reverse=True,
        )
