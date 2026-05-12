import numpy as np


def ema(prices: list, period: int) -> np.ndarray:
    arr = np.array(prices, dtype=float)
    k   = 2.0 / (period + 1)
    out = np.full(len(arr), np.nan)
    # Seed with SMA
    if len(arr) < period:
        return out
    out[period - 1] = arr[:period].mean()
    for i in range(period, len(arr)):
        out[i] = arr[i] * k + out[i - 1] * (1 - k)
    return out


def rsi(prices: list, period: int = 14) -> np.ndarray:
    arr  = np.array(prices, dtype=float)
    diff = np.diff(arr)
    gains = np.where(diff > 0, diff, 0.0)
    loss  = np.where(diff < 0, -diff, 0.0)
    out  = np.full(len(arr), 50.0)
    if len(arr) <= period:
        return out
    avg_g = gains[:period].mean()
    avg_l = loss[:period].mean()
    for i in range(period, len(diff)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + loss[i])  / period
        rs = avg_g / (avg_l + 1e-10)
        out[i + 1] = 100 - 100 / (1 + rs)
    return out


def atr(highs: list, lows: list, closes: list, period: int = 14) -> np.ndarray:
    h = np.array(highs,  dtype=float)
    l = np.array(lows,   dtype=float)
    c = np.array(closes, dtype=float)
    prev_c = np.roll(c, 1); prev_c[0] = c[0]
    tr  = np.maximum(h - l, np.maximum(np.abs(h - prev_c), np.abs(l - prev_c)))
    out = np.full(len(c), tr.mean())
    avg = tr[:period].mean()
    for i in range(period, len(tr)):
        avg = (avg * (period - 1) + tr[i]) / period
        out[i] = avg
    return out


def macd(prices: list, fast=12, slow=26, sig=9):
    e_fast = ema(prices, fast)
    e_slow = ema(prices, slow)
    line   = e_fast - e_slow
    valid  = ~np.isnan(line)
    signal = np.full(len(line), np.nan)
    if valid.sum() > sig:
        idx = np.where(valid)[0]
        s   = ema(line[valid].tolist(), sig)
        signal[idx] = s
    hist = line - signal
    return line, signal, hist
