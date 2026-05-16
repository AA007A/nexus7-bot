"""
Análise técnica multi-indicador — sem mínimos hardcoded
Filtra por Risk/Reward ratio mínimo
"""
import numpy as np
from dataclasses import dataclass, field
from typing import Optional
from bot.indicators import ema, rsi, atr, macd
from bot.config import cfg


@dataclass
class Signal:
    symbol:     str
    direction:  str
    entry:      float
    sl:         float
    tp:         float
    confidence: float
    reason:     str = ""
    rr:         float = field(init=False)

    def __post_init__(self):
        risk   = abs(self.entry - self.sl)
        reward = abs(self.tp   - self.entry)
        self.rr = round(reward / risk, 2) if risk > 0 else 0


class Analyzer:
    def analyze(self, symbol: str, klines: list) -> Optional[Signal]:
        if len(klines) < 50:
            return None

        closes = [k["c"] for k in klines]
        highs  = [k["h"] for k in klines]
        lows   = [k["l"] for k in klines]
        price  = closes[-1]
        atr_v  = atr(highs, lows, closes)[-1]

        if atr_v <= 0 or price <= 0:
            return None

        ema9   = ema(closes, 9)[-1]
        ema21  = ema(closes, 21)[-1]
        ema50  = ema(closes, 50)[-1]
        rsi_v  = rsi(closes)[-1]
        _, _, hist = macd(closes)
        mh     = hist[-1] if not np.isnan(hist[-1]) else 0
        prev_mh = hist[-2] if len(hist) > 1 and not np.isnan(hist[-2]) else mh

        long_s, short_s = 0, 0
        rl, rs = [], []

        # EMA stack
        if ema9 > ema21 > ema50:
            long_s += 2; rl.append("EMA stack ▲")
        if ema9 < ema21 < ema50:
            short_s += 2; rs.append("EMA stack ▼")

        # Price vs EMA21
        if price > ema21: long_s  += 1
        if price < ema21: short_s += 1

        # RSI
        if rsi_v < 35:    long_s  += 2; rl.append(f"RSI {rsi_v:.0f}")
        elif rsi_v < 45:  long_s  += 1
        if rsi_v > 65:    short_s += 2; rs.append(f"RSI {rsi_v:.0f}")
        elif rsi_v > 55:  short_s += 1

        # MACD
        if mh > 0 and mh > prev_mh: long_s  += 2; rl.append("MACD ↑")
        if mh < 0 and mh < prev_mh: short_s += 2; rs.append("MACD ↓")

        # Momentum (últimas 3 velas)
        if len(closes) >= 4:
            mom = (closes[-1] - closes[-4]) / closes[-4]
            if mom >  0.002: long_s  += 1
            if mom < -0.002: short_s += 1

        # Gera sinal
        max_s = 8
        if long_s >= 5 and long_s > short_s:
            conf = min(0.95, 0.55 + (long_s / max_s) * 0.45)
            sl   = price - atr_v * 1.5
            tp   = price + atr_v * 3.0
            sig = Signal(symbol, "LONG", price, sl, tp, conf, " | ".join(rl[:3]))
            
            # Filtra por RR ratio mínimo
            if sig.rr >= cfg.MIN_RR_RATIO:
                return sig
            else:
                return None

        if short_s >= 5 and short_s > long_s:
            conf = min(0.95, 0.55 + (short_s / max_s) * 0.45)
            sl   = price + atr_v * 1.5
            tp   = price - atr_v * 3.0
            sig = Signal(symbol, "SHORT", price, sl, tp, conf, " | ".join(rs[:3]))
            
            # Filtra por RR ratio mínimo
            if sig.rr >= cfg.MIN_RR_RATIO:
                return sig
            else:
                return None

        return None
