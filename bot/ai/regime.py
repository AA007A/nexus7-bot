"""Explicit regime classification for the AI decision layer.

Deterministic, CLOSED 1h candles only (the research classifier, which already
refuses short histories). UNKNOWN and EXTREME are never tradable.
"""
from __future__ import annotations

REGIMES = ("TREND_UP", "TREND_DOWN", "RANGE", "HIGH_VOLATILITY", "LOW_VOLATILITY",
           "BREAKOUT", "CHOP", "EXTREME", "UNKNOWN")
NEVER_TRADABLE = frozenset({"UNKNOWN", "EXTREME"})

_MAP = {
    "TRENDING_BULL": "TREND_UP", "TRENDING_BEAR": "TREND_DOWN", "RANGE": "RANGE",
    "HIGH_VOLATILITY": "HIGH_VOLATILITY", "LOW_VOLATILITY": "LOW_VOLATILITY",
    "BREAKOUT": "BREAKOUT", "BREAKDOWN": "BREAKOUT", "CHOPPY": "CHOP",
    "EXTREME_EVENT": "EXTREME", "UNKNOWN": "UNKNOWN",
}


def classify(window_1h) -> str:
    from bot import nexus_oos_research as res
    return _MAP.get(res.classify_regime(list(window_1h or ())), "UNKNOWN")


def from_research_label(label: str | None) -> str:
    return _MAP.get(str(label or "UNKNOWN"), "UNKNOWN")
