"""
KAKAZITO TRADE Strategy v7.0 — Score 0-100
Critérios de entrada:
  - EMA stack (20/50/200) — tendência
  - RSI calibrado (evita extremos)
  - MACD com histograma
  - Volume acima da média
  - ATR / Volatilidade moderada
  - Detecção de fake breakout / manipulação
  - Relação risco/retorno >= 2:1
  - Score mínimo: 80/100 para operar
"""
import numpy as np
from dataclasses import dataclass, field
from typing import Optional
from bot.indicators import ema, rsi, atr, macd
from bot.logger import log


@dataclass
class Signal:
    symbol:     str
    direction:  str       # "LONG" | "SHORT"
    entry:      float
    sl:         float
    tp:         float
    confidence: float     # 0.0 – 1.0
    reason:     str = ""
    score:      int = 0   # 0-100
    rr:         float = field(init=False)

    def __post_init__(self):
        risk   = abs(self.entry - self.sl)
        reward = abs(self.tp   - self.entry)
        self.rr = round(reward / risk, 2) if risk > 0 else 0


class Analyzer:
    def analyze(self, symbol: str, klines: list) -> Optional[Signal]:
        if len(klines) < 60:
            return None

        closes  = [k["c"] for k in klines]
        highs   = [k["h"] for k in klines]
        lows    = [k["l"] for k in klines]
        opens   = [k["o"] for k in klines]
        volumes = [k["v"] for k in klines]
        price   = closes[-1]

        atr_arr = atr(highs, lows, closes)
        atr_v   = atr_arr[-1]
        if atr_v <= 0 or price <= 0:
            return None

        # ── EMAs ──────────────────────────────────────────────
        ema20  = ema(closes, 20)
        ema50  = ema(closes, 50)
        ema200 = ema(closes, 200) if len(closes) >= 200 else ema(closes, 50)

        e20, e50, e200 = ema20[-1], ema50[-1], ema200[-1]
        bullish_stack = (not np.isnan(e20)) and (not np.isnan(e50)) and e20 > e50 > e200 and price > e20
        bearish_stack = (not np.isnan(e20)) and (not np.isnan(e50)) and e20 < e50 < e200 and price < e20

        # ── Score: Tendência (25 pts) ──────────────────────────
        if bullish_stack or bearish_stack:
            trend_score = 25
        elif (e20 > e50 or e20 < e50):
            trend_score = 10   # tendência parcial
        else:
            trend_score = 0

        direction = "LONG" if (bullish_stack or (e20 > e50 and not bearish_stack)) else "SHORT"

        # ── Score: RSI (20 pts) ────────────────────────────────
        rsi_arr = rsi(closes)
        rsi_v   = rsi_arr[-1]

        if direction == "LONG":
            if 40 <= rsi_v <= 62:    rsi_score = 20
            elif 30 <= rsi_v < 40:   rsi_score = 12
            elif 62 < rsi_v <= 70:   rsi_score = 8
            else:                    rsi_score = 0   # >70 sobrecomprado → skip
        else:
            if 38 <= rsi_v <= 60:    rsi_score = 20
            elif 60 < rsi_v <= 70:   rsi_score = 12
            elif 30 <= rsi_v < 38:   rsi_score = 8
            else:                    rsi_score = 0   # <30 sobrevendido → skip

        # ── Score: MACD (20 pts) ───────────────────────────────
        _, _, hist_arr = macd(closes)
        mh      = hist_arr[-1] if not np.isnan(hist_arr[-1]) else 0
        prev_mh = hist_arr[-2] if len(hist_arr) > 1 and not np.isnan(hist_arr[-2]) else mh

        if direction == "LONG":
            if mh > 0 and mh > prev_mh:   macd_score = 20
            elif mh > 0:                   macd_score = 10
            elif mh > prev_mh:             macd_score = 5
            else:                          macd_score = 0
        else:
            if mh < 0 and mh < prev_mh:   macd_score = 20
            elif mh < 0:                   macd_score = 10
            elif mh < prev_mh:             macd_score = 5
            else:                          macd_score = 0

        # ── Score: Volume (15 pts) ─────────────────────────────
        vols    = np.array(volumes, dtype=float)
        avg_vol = vols[-21:-1].mean() if len(vols) > 21 else vols.mean()
        avg_vol = avg_vol or 1
        vol_ratio = vols[-1] / avg_vol

        if vol_ratio >= 2.0:   vol_score = 15
        elif vol_ratio >= 1.5: vol_score = 10
        elif vol_ratio >= 1.1: vol_score = 5
        else:                  vol_score = 0

        # ── Score: Volatilidade / ATR (10 pts) ─────────────────
        atr_pct = atr_v / price * 100
        if 0.3 <= atr_pct <= 2.5:   atr_score = 10
        elif 0.2 <= atr_pct < 0.3:  atr_score = 5
        elif 2.5 < atr_pct <= 4.0:  atr_score = 5
        else:                        atr_score = 0

        # ── Score: Manipulação / Fake Breakout (10 pts) ─────────
        manip_score = 10
        body        = abs(closes[-1] - opens[-1])
        candle_range= highs[-1] - lows[-1]
        wick_ratio  = 1 - (body / candle_range) if candle_range > 0 else 1

        # Vela com >70% de sombra → possível manipulação
        if wick_ratio > 0.70:
            manip_score -= 7

        # Volume spike sem movimento de preço → manipulação de liquidez
        if vol_ratio > 3.0 and body < atr_v * 0.25:
            manip_score -= 5

        # Breakout falso: preço rompeu high/low mas fechou de volta
        prev_high = max(highs[-6:-1])
        prev_low  = min(lows[-6:-1])
        if direction == "LONG" and highs[-1] > prev_high and closes[-1] < prev_high:
            manip_score -= 5   # fake breakout de alta

        if direction == "SHORT" and lows[-1] < prev_low and closes[-1] > prev_low:
            manip_score -= 5   # fake breakout de baixa

        manip_score = max(0, manip_score)

        # ── Total ──────────────────────────────────────────────
        total = trend_score + rsi_score + macd_score + vol_score + atr_score + manip_score

        reasons = []
        if bullish_stack or bearish_stack: reasons.append("EMA✓")
        if rsi_score >= 12:  reasons.append(f"RSI {rsi_v:.0f}")
        if macd_score >= 10: reasons.append("MACD✓")
        if vol_score >= 10:  reasons.append(f"VOL x{vol_ratio:.1f}")
        if manip_score == 10:reasons.append("Clean")

        # ── SL / TP baseado em ATR ─────────────────────────────
        if direction == "LONG":
            sl = price - atr_v * 1.5
            tp = price + atr_v * 3.0
        else:
            sl = price + atr_v * 1.5
            tp = price - atr_v * 3.0

        # Verifica R:R >= 2
        risk   = abs(price - sl)
        reward = abs(tp - price)
        rr     = reward / risk if risk > 0 else 0
        if rr < 2.0:
            log.debug(f"[{symbol}] R:R {rr:.2f} < 2.0 → skip")
            return None

        confidence = min(0.98, total / 100)

        log.info(
            f"[{symbol}] Score={total}/100 {direction} | "
            f"RSI={rsi_v:.0f} MACD={'↑' if mh > 0 else '↓'} "
            f"VOL={vol_ratio:.1f}x ATR%={atr_pct:.2f} | {' '.join(reasons)}"
        )

        return Signal(
            symbol=symbol,
            direction=direction,
            entry=price,
            sl=round(sl, 6),
            tp=round(tp, 6),
            confidence=confidence,
            reason=" | ".join(reasons),
            score=int(total),
        )

    def rank_signals(self, signals: list) -> list:
        """Ordena sinais por score decrescente."""
        return sorted([s for s in signals if s], key=lambda s: s.score, reverse=True)
