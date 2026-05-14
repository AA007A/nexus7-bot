"""
Análise técnica multi-indicador
"""
import numpy as np
from dataclasses import dataclass, field
from typing import Optional
from bot.indicators import ema, rsi, atr, macd

# Quantidade mínima por símbolo na Bybit
MIN_QTY = {
    "BTCUSDT":  0.001,
    "ETHUSDT":  0.001,   # mínimo real Bybit
    "SOLUSDT":  0.01,    # mínimo real Bybit
    "BNBUSDT":  0.001,   # mínimo real Bybit
    "XRPUSDT":  1.0,     # mínimo real Bybit
    "AVAXUSDT": 0.01,
    "DOGEUSDT": 1.0,
    "LINKUSDT": 0.01,
}

# Valor mínimo notional (USD) por símbolo
MIN_NOTIONAL = {
    "BTCUSDT":  5.0,
    "ETHUSDT":  5.0,
    "SOLUSDT":  5.0,
    "BNBUSDT":  5.0,
    "XRPUSDT":  5.0,
    "AVAXUSDT": 5.0,
    "DOGEUSDT": 5.0,
    "LINKUSDT": 5.0,
}


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
        reward = abs(self.tp - self.entry)
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

        ema9  = ema(closes, 9)[-1]
        ema21 = ema(closes, 21)[-1]
        ema50 = ema(closes, 50)[-1]
        rsi_v = rsi(closes)[-1]
        macd_line, macd_sig, macd_hist = macd(closes)
        mh = macd_hist[-1]
        prev_mh = macd_hist[-2] if len(macd_hist) > 1 else mh

        long_score = 0
        short_score = 0
        reasons_l = []
        reasons_s = []

        if ema9 > ema21 > ema50:
            long_score += 2; reasons_l.append("EMA bullish")
        if ema9 < ema21 < ema50:
            short_score += 2; reasons_s.append("EMA bearish")

        if price > ema21:
            long_score += 1
        if price < ema21:
            short_score += 1

        if rsi_v < 40:
            long_score += 2; reasons_l.append(f"RSI {rsi_v:.0f}")
        if rsi_v > 60:
            short_score += 2; reasons_s.append(f"RSI {rsi_v:.0f}")

        if not np.isnan(mh):
            if mh > 0 and mh > prev_mh:
                long_score += 2; reasons_l.append("MACD↑")
            if mh < 0 and mh < prev_mh:
                short_score += 2; reasons_s.append("MACD↓")

        if len(closes) >= 4:
            mom = (closes[-1] - closes[-4]) / closes[-4]
            if mom > 0.002: long_score += 1
            if mom < -0.002: short_score += 1

        if long_score >= 5 and long_score > short_score:
            conf = min(0.92, 0.55 + (long_score / 8) * 0.4)
            sl = price - atr_v * 1.2
            tp = price + atr_v * 2.5
            return Signal(symbol, "LONG", price, sl, tp, conf,
                          " | ".join(reasons_l[:3]))

        if short_score >= 5 and short_score > long_score:
            conf = min(0.92, 0.55 + (short_score / 8) * 0.4)
            sl = price + atr_v * 1.2
            tp = price - atr_v * 2.5
            return Signal(symbol, "SHORT", price, sl, tp, conf,
                          " | ".join(reasons_s[:3]))

        return None
