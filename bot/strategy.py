"""
Análise técnica multi-indicador para gerar sinais de alta qualidade.
Confluência de sinais = maior precisão.
"""
import numpy as np
from dataclasses import dataclass, field
from typing import Optional
from bot.indicators import ema, rsi, atr, macd


@dataclass
class Signal:
    symbol:     str
    direction:  str        # "LONG" or "SHORT"
    entry:      float
    sl:         float
    tp:         float
    confidence: float      # 0.0 – 1.0
    reason:     str = ""
    rr:         float = field(init=False)

    def __post_init__(self):
        risk   = abs(self.entry - self.sl)
        reward = abs(self.tp   - self.entry)
        self.rr = round(reward / risk, 2) if risk > 0 else 0


class Analyzer:
    """Gera sinais por confluência de múltiplos indicadores"""

    def analyze(self, symbol: str, klines: list) -> Optional[Signal]:
        if len(klines) < 50:
            return None

        closes = [k["c"] for k in klines]
        highs  = [k["h"] for k in klines]
        lows   = [k["l"] for k in klines]
        price  = closes[-1]
        atr_v  = atr(highs, lows, closes)[-1]

        # ── Indicadores ──────────────────────────────────────
        ema9  = ema(closes, 9)[-1]
        ema21 = ema(closes, 21)[-1]
        ema50 = ema(closes, 50)[-1]
        rsi_v = rsi(closes)[-1]
        macd_line, macd_sig, macd_hist = macd(closes)
        ml = macd_line[-1]; ms = macd_sig[-1]; mh = macd_hist[-1]
        prev_mh = macd_hist[-2] if len(macd_hist) > 1 else mh

        # ── Scoring: LONG ────────────────────────────────────
        long_score  = 0
        short_score = 0
        reasons_l   = []
        reasons_s   = []

        # 1. EMA stack
        if ema9 > ema21 > ema50:
            long_score += 2; reasons_l.append("EMA bullish stack")
        if ema9 < ema21 < ema50:
            short_score += 2; reasons_s.append("EMA bearish stack")

        # 2. Price vs EMA
        if price > ema21:
            long_score += 1; reasons_l.append("price>EMA21")
        if price < ema21:
            short_score += 1; reasons_s.append("price<EMA21")

        # 3. RSI zones
        if 40 <= rsi_v <= 60:
            long_score += 1; short_score += 1   # neutro
        if rsi_v < 40:
            long_score += 2; reasons_l.append(f"RSI oversold {rsi_v:.0f}")
        if rsi_v > 60:
            short_score += 2; reasons_s.append(f"RSI overbought {rsi_v:.0f}")
        if rsi_v < 25:  # extremo
            long_score += 1
        if rsi_v > 75:
            short_score += 1

        # 4. MACD
        if not np.isnan(ml) and not np.isnan(ms):
            if ml > ms and mh > 0 and mh > prev_mh:
                long_score += 2; reasons_l.append("MACD cross UP")
            if ml < ms and mh < 0 and mh < prev_mh:
                short_score += 2; reasons_s.append("MACD cross DOWN")

        # 5. Momentum (last 3 candles)
        if len(closes) >= 4:
            momentum = (closes[-1] - closes[-4]) / closes[-4]
            if momentum > 0.003:
                long_score += 1
            if momentum < -0.003:
                short_score += 1

        # ── Gerar sinal ──────────────────────────────────────
        max_score = 8
        if long_score >= 5 and long_score > short_score:
            conf = min(0.92, 0.55 + (long_score / max_score) * 0.4)
            sl   = price - atr_v * 1.2
            tp   = price + atr_v * 2.5
            return Signal(symbol, "LONG", price, sl, tp, conf,
                          " | ".join(reasons_l[:3]))

        if short_score >= 5 and short_score > long_score:
            conf = min(0.92, 0.55 + (short_score / max_score) * 0.4)
            sl   = price + atr_v * 1.2
            tp   = price - atr_v * 2.5
            return Signal(symbol, "SHORT", price, sl, tp, conf,
                          " | ".join(reasons_s[:3]))

        return None
