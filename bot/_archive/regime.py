"""
BGX Capital — Regime Classifier v2.0
Classifica: TRENDING_UP / TRENDING_DOWN / RANGING / COMPRESSED / CHOPPY
Adicionado: COMPRESSED para volatilidade muito baixa (Bollinger squeeze)
Adicionado: volatility_rank para sizing dinâmico baseado em ATR percentil
"""
import numpy as np
from bot.logger import log


def classify(closes: list, highs: list, lows: list, symbol: str = "") -> dict:
    if len(closes) < 20:
        return {
            "regime": "UNKNOWN", "strategy": "HOLD",
            "adx": 0, "ci": 50, "atr_pct": 0,
            "tradeable": False, "volatility_rank": 50,
            "size_mult": 1.0,
        }

    # ── ADX ──────────────────────────────────────────────────────
    try:
        from bot.indicators import adx as af
        d       = af(highs, lows, closes)
        adx_v   = d["adx"]
        adx_dir = d["direction"]
    except Exception:
        adx_v, adx_dir = 20, "LONG"

    # ── ATR ──────────────────────────────────────────────────────
    try:
        from bot.indicators import atr as atrf
        a          = atrf(highs, lows, closes)
        av         = float(a[-1])
        avg_14     = float(np.mean(a[-14:])) if len(a) >= 14 else av
        atr_expand = av > avg_14 * 1.03
        atr_pct    = av / closes[-1] * 100 if closes[-1] > 0 else 0

        # Volatility rank: percentil do ATR atual vs últimos 100 valores
        if len(a) >= 20:
            window     = a[-100:] if len(a) >= 100 else a
            vol_rank   = float(np.sum(window < av) / len(window) * 100)
        else:
            vol_rank   = 50.0
    except Exception:
        av, avg_14, atr_expand, atr_pct, vol_rank = 0, 0, False, 0, 50

    # ── Choppiness ───────────────────────────────────────────────
    try:
        from bot.indicators import choppiness as cf
        cd      = cf(highs, lows, closes)
        ci_chop = cd["chop"]
        ci_v    = cd["ci"]
    except Exception:
        ci_chop, ci_v = False, 50

    # ── Bollinger Bands ───────────────────────────────────────────
    try:
        from bot.indicators import bollinger
        bb    = bollinger(closes)
        bb_sq = bb["squeezed"]
        bb_w  = bb["width"]
    except Exception:
        bb_sq, bb_w = False, 3.0

    # ── Classificação de regime ───────────────────────────────────
    # Ordem: verificações da mais restritiva para a mais permissiva
    if bb_sq and adx_v < 20 and vol_rank < 20:
        # Volatilidade muito baixa + BB squeeze + sem tendência
        regime   = "COMPRESSED"
        strategy = "HOLD"

    elif (avg_14 > 0 and av < avg_14 * 0.70) or (ci_chop and adx_v < 18):
        # ATR muito abaixo da média OU choppiness alto + sem tendência
        regime   = "CHOPPY"
        strategy = "HOLD"

    elif adx_v > 25 and atr_expand and not ci_chop:
        # Tendência forte com expansão de ATR e baixo choppiness
        regime   = "TRENDING_UP" if adx_dir == "LONG" else "TRENDING_DOWN"
        strategy = "TREND_FOLLOW"

    elif adx_v < 20:
        # ADX baixo — mercado lateral
        regime   = "RANGING"
        strategy = "MEAN_REVERSION"

    else:
        # ADX entre 20-25 — transição ou neutro
        regime   = "NEUTRAL"
        strategy = "SELECTIVE"

    # ── Size mult baseado em volatility rank ──────────────────────
    # Alta volatilidade (vol_rank > 80) → reduz tamanho para 70%
    # Baixa volatilidade (vol_rank < 20) → reduz para 80% (spread/slippage altos)
    # Normal (20-80) → tamanho completo
    if vol_rank > 80:
        size_mult = 0.70
    elif vol_rank < 20:
        size_mult = 0.80
    else:
        size_mult = 1.00

    tradeable = regime not in ("CHOPPY", "COMPRESSED", "UNKNOWN")

    if symbol:
        log.debug(
            f"[{symbol}] Regime={regime} ADX={adx_v:.1f} CI={ci_v:.1f} "
            f"ATR%={atr_pct:.2f} VolRank={vol_rank:.0f} size={size_mult:.0%}"
        )

    return {
        "regime":         regime,
        "strategy":       strategy,
        "adx":            round(adx_v, 1),
        "adx_dir":        adx_dir,
        "ci":             round(ci_v, 1),
        "atr_pct":        round(atr_pct, 3),
        "atr_expand":     atr_expand,
        "bb_squeeze":     bb_sq,
        "bb_width":       round(bb_w, 2),
        "volatility_rank": round(vol_rank, 1),
        "size_mult":      size_mult,
        "tradeable":      tradeable,
    }
