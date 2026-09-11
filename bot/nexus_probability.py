"""Versioned EV heuristic, not an empirically calibrated probability.

OOS reconstructs this transform from stored confidence rounded to two decimals,
with a maximum probability rounding error of 0.0000225.
"""
from math import isfinite

PROBABILITY_MODEL = "ensemble_linear_v1"


def heuristic_win_probability(confidence: float) -> float:
    if isinstance(confidence, bool):
        raise ValueError("confidence must be numeric")
    value = float(confidence)
    if not isfinite(value) or not 0.0 <= value <= 100.0:
        raise ValueError("confidence must be finite and within 0..100")
    return min(0.75, 0.30 + (value / 100) * 0.45)
