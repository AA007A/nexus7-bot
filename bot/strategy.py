"""
KAKAZITO TRADE Strategy v8.0 — Multi-Timeframe (15m + 1h)
═══════════════════════════════════════════════════════════
Lógica de entrada:
  1. Tendência do 1H define o VIÉS (Long ou Short)
  2. 15M confirma o TIMING de entrada (pullback, momentum)
  3. Score 0-100 com 7 critérios combinados
  4. Sinal só emitido quando 1H e 15M apontam na MESMA direção
  5. R:R >= 2:1 obrigatório

Score breakdown (100 pts total):
  - Tendência 1H  (25 pts) — EMA 20/50/200 no timeframe maior
  - Tendência 15M (15 pts) — EMA 20/50 no timeframe menor
  - RSI           (15 pts) — zona ideal, evita extremos
  - MACD          (15 pts) — histograma + cruzamento
  - Volume        (15 pts) — confirmação de força
  - ATR/Volatil.  ( 8 pts) — volatilidade moderada ideal
  - Limpeza       ( 7 pts) — sem fake breakout/manipulação
"""
import numpy as np
from dataclasses import dataclass, field
from typing import Optional, Dict
from bot.indicators import ema, rsi, atr, macd
from bot.logger import log


@dataclass
class Signal:
    symbol:     str
    direction:  str        # "LONG" | "SHORT"
    entry:      float
    sl:         float
    tp:         float
    confidence: float      # 0.0 – 1.0
    reason:     str = ""
    score:      int = 0    # 0-100
    tf_1h:      str = ""   # resumo do 1H
    tf_15m:     str = ""   # resumo do 15M
    rr:         float = field(init=False)

    def __post_init__(self):
        risk   = abs(self.entry - self.sl)
        reward = abs(self.tp   - self.entry)
        self.rr = round(reward / risk, 2) if risk > 0 else 0


def _score_timeframe(closes, highs, lows, opens, volumes, label: str) -> dict:
    """
    Analisa um único timeframe e retorna scores parciais + direção.
    """
    price  = closes[-1]
    atr_v  = atr(highs, lows, closes)[-1]
    if atr_v <= 0 or price <= 0:
        return {"ok": False}

    # ── EMAs ──────────────────────────────────────────────────
    e20  = ema(closes, 20)[-1]
    e50  = ema(closes, 50)[-1]
    e200 = ema(closes, min(200, len(closes)-1))[-1]

    bull = (not np.isnan(e20)) and (not np.isnan(e50)) and e20 > e50 and price > e20
    bear = (not np.isnan(e20)) and (not np.isnan(e50)) and e20 < e50 and price < e20
    full_bull = bull and (not np.isnan(e200)) and e50 > e200   # EMA stack completa
    full_bear = bear and (not np.isnan(e200)) and e50 < e200

    direction = "LONG" if bull else "SHORT"

    # ── RSI ───────────────────────────────────────────────────
    rsi_v = rsi(closes)[-1]
    if direction == "LONG":
        if 42 <= rsi_v <= 62:    rsi_s = 15
        elif 35 <= rsi_v < 42:   rsi_s = 8
        elif 62 < rsi_v <= 68:   rsi_s = 5
        else:                    rsi_s = 0   # sobrecomprado/vendido → bloqueia
    else:
        if 38 <= rsi_v <= 58:    rsi_s = 15
        elif 58 < rsi_v <= 65:   rsi_s = 8
        elif 32 <= rsi_v < 38:   rsi_s = 5
        else:                    rsi_s = 0

    # ── MACD ──────────────────────────────────────────────────
    _, sig_line, hist_arr = macd(closes)
    h0 = hist_arr[-1] if not np.isnan(hist_arr[-1]) else 0
    h1 = hist_arr[-2] if len(hist_arr) > 1 and not np.isnan(hist_arr[-2]) else h0

    if direction == "LONG":
        if h0 > 0 and h0 > h1:   macd_s = 15   # acelerando positivo
        elif h0 > 0:              macd_s = 8    # positivo mas perdendo força
        elif h0 > h1:             macd_s = 4    # cruzando para cima
        else:                     macd_s = 0
    else:
        if h0 < 0 and h0 < h1:   macd_s = 15
        elif h0 < 0:              macd_s = 8
        elif h0 < h1:             macd_s = 4
        else:                     macd_s = 0

    # ── Volume ────────────────────────────────────────────────
    vols    = np.array(volumes, dtype=float)
    avg_vol = vols[-21:-1].mean() if len(vols) > 21 else vols.mean() or 1
    vol_r   = vols[-1] / avg_vol

    if vol_r >= 2.0:   vol_s = 15
    elif vol_r >= 1.5: vol_s = 10
    elif vol_r >= 1.1: vol_s = 5
    else:              vol_s = 0

    # ── ATR/Volatilidade ──────────────────────────────────────
    atr_pct = atr_v / price * 100
    if 0.25 <= atr_pct <= 2.5:   atr_s = 8
    elif 0.15 <= atr_pct < 0.25: atr_s = 4
    elif 2.5 < atr_pct <= 4.0:   atr_s = 4
    else:                         atr_s = 0

    # ── Limpeza / Anti-manipulação ────────────────────────────
    body    = abs(closes[-1] - opens[-1])
    cr      = highs[-1] - lows[-1]
    wick_r  = 1 - (body / cr) if cr > 0 else 1
    clean_s = 7
    if wick_r > 0.70:                               clean_s -= 5   # vela com wick dominante
    if vol_r > 3.0 and body < atr_v * 0.20:        clean_s -= 4   # spike sem corpo
    ph = max(highs[-6:-1]) if len(highs) > 6 else highs[-1]
    pl = min(lows[-6:-1])  if len(lows)  > 6 else lows[-1]
    if direction == "LONG"  and highs[-1] > ph and closes[-1] < ph: clean_s -= 3
    if direction == "SHORT" and lows[-1]  < pl and closes[-1] > pl: clean_s -= 3
    clean_s = max(0, clean_s)

    summary = (
        f"{'↑' if bull else '↓'}EMA "
        f"RSI{rsi_v:.0f} "
        f"{'M+' if macd_s >= 8 else 'M-'} "
        f"V{vol_r:.1f}x"
    )

    return {
        "ok":        True,
        "direction": direction,
        "bull":      bull,
        "bear":      bear,
        "full_bull": full_bull,
        "full_bear": full_bear,
        "rsi_v":     rsi_v,
        "rsi_s":     rsi_s,
        "macd_s":    macd_s,
        "vol_s":     vol_s,
        "atr_s":     atr_s,
        "clean_s":   clean_s,
        "vol_r":     vol_r,
        "atr_v":     atr_v,
        "price":     price,
        "summary":   summary,
    }


class Analyzer:
    def analyze_mtf(
        self,
        symbol:    str,
        k15:       list,   # klines 15 minutos (100 candles)
        k1h:       list,   # klines 1 hora     (100 candles)
    ) -> Optional[Signal]:
        """
        Análise Multi-Timeframe:
          1H  → define o VIÉS (tendência maior)
          15M → confirma TIMING (entrada precisa)
        Sinal só emitido se ambos apontam na mesma direção.
        """
        if len(k15) < 60 or len(k1h) < 30:
            return None

        def klines_to_arrays(klines):
            return (
                [k["c"] for k in klines],
                [k["h"] for k in klines],
                [k["l"] for k in klines],
                [k["o"] for k in klines],
                [k["v"] for k in klines],
            )

        c15, h15, l15, o15, v15 = klines_to_arrays(k15)
        c1h, h1h, l1h, o1h, v1h = klines_to_arrays(k1h)

        tf1h  = _score_timeframe(c1h, h1h, l1h, o1h, v1h, "1H")
        tf15m = _score_timeframe(c15, h15, l15, o15, v15, "15M")

        if not tf1h["ok"] or not tf15m["ok"]:
            return None

        price = tf15m["price"]
        atr_v = tf15m["atr_v"]

        # ── REGRA PRINCIPAL: 1H e 15M devem concordar ─────────
        dir_1h  = tf1h["direction"]
        dir_15m = tf15m["direction"]

        if dir_1h != dir_15m:
            log.debug(
                f"[{symbol}] MTF conflito: 1H={dir_1h} vs 15M={dir_15m} → SKIP"
            )
            return None

        direction = dir_1h   # ambos concordam

        # ── SCORE COMBINADO ───────────────────────────────────
        # Tendência 1H (25 pts) — peso maior pois define o viés
        if tf1h["full_bull"] or tf1h["full_bear"]:
            t1h_s = 25   # EMA stack completa no 1H
        elif tf1h["bull"] or tf1h["bear"]:
            t1h_s = 16   # tendência parcial no 1H
        else:
            t1h_s = 0

        # Tendência 15M (15 pts) — confirmação de timing
        if tf15m["full_bull"] or tf15m["full_bear"]:
            t15_s = 15
        elif tf15m["bull"] or tf15m["bear"]:
            t15_s = 9
        else:
            t15_s = 0

        # Resto: média ponderada entre 1H e 15M
        rsi_s   = round((tf1h["rsi_s"]   * 0.4 + tf15m["rsi_s"]   * 0.6))
        macd_s  = round((tf1h["macd_s"]  * 0.4 + tf15m["macd_s"]  * 0.6))
        vol_s   = round((tf1h["vol_s"]   * 0.3 + tf15m["vol_s"]   * 0.7))
        atr_s   = tf15m["atr_s"]
        clean_s = round((tf1h["clean_s"] * 0.3 + tf15m["clean_s"] * 0.7))

        total = t1h_s + t15_s + rsi_s + macd_s + vol_s + atr_s + clean_s

        # ── FILTROS EXTRAS DE QUALIDADE ───────────────────────
        # RSI extremo: só cancela se AMBOS os TFs estiverem em zona extrema
        # (1 TF pode estar neutro enquanto o outro confirma)
        if tf1h["rsi_s"] == 0 and tf15m["rsi_s"] == 0:
            log.debug(f"[{symbol}] RSI extremo em ambos TFs → SKIP")
            return None
        # Se RSI do 15M (timing) estiver extremo, não entra
        if tf15m["rsi_s"] == 0:
            log.debug(f"[{symbol}] RSI 15M extremo → SKIP")
            return None

        # MACD deve ser positivo em pelo menos 1 TF
        if tf1h["macd_s"] == 0 and tf15m["macd_s"] == 0:
            log.debug(f"[{symbol}] MACD sem sinal → SKIP")
            return None

        # ── SL / TP com ATR do 15M ───────────────────────────
        # Usa ATR do 1H para SL mais folgado (menos ruído)
        atr_1h = atr(h1h, l1h, c1h)[-1]

        if direction == "LONG":
            sl = round(price - atr_1h * 1.5, 6)   # SL abaixo do ATR 1H
            tp = round(price + atr_1h * 3.0, 6)   # TP = 2x o risco (R:R 2:1)
        else:
            sl = round(price + atr_1h * 1.5, 6)
            tp = round(price - atr_1h * 3.0, 6)

        # ── R:R mínimo 2:1 ────────────────────────────────────
        risk   = abs(price - sl)
        reward = abs(tp - price)
        rr     = reward / risk if risk > 0 else 0
        if rr < 2.0:
            log.debug(f"[{symbol}] R:R {rr:.2f} < 2.0 → SKIP")
            return None

        # ── Monta razões do sinal ─────────────────────────────
        reasons = []
        if t1h_s >= 16:  reasons.append("1H-EMA✓")
        if t15_s >= 9:   reasons.append("15M-EMA✓")
        if rsi_s >= 10:  reasons.append(f"RSI-ok({tf15m['rsi_v']:.0f})")
        if macd_s >= 8:  reasons.append("MACD✓")
        if vol_s >= 10:  reasons.append(f"VOL-{tf15m['vol_r']:.1f}x")
        if clean_s >= 5: reasons.append("Clean")

        confidence = min(0.98, total / 100)

        log.info(
            f"[{symbol}] MTF ✅ {direction} score={total}/100 "
            f"1H:[{tf1h['summary']}] 15M:[{tf15m['summary']}] "
            f"R:R={rr:.1f} | {' '.join(reasons)}"
        )

        return Signal(
            symbol=symbol,
            direction=direction,
            entry=price,
            sl=sl,
            tp=tp,
            confidence=confidence,
            reason=" | ".join(reasons),
            score=int(total),
            tf_1h=tf1h["summary"],
            tf_15m=tf15m["summary"],
        )

    # Mantém compatibilidade com o engine (que chama analyze)
    def analyze(self, symbol: str, klines: list) -> Optional[Signal]:
        """Fallback single-TF — usado só se o engine não tiver 1H disponível."""
        return self.analyze_mtf(symbol, klines, klines)

    def rank_signals(self, signals: list) -> list:
        return sorted([s for s in signals if s], key=lambda s: s.score, reverse=True)
