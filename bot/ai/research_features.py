"""RESEARCH-ONLY decision-time features (Phase 8D, SIGNAL_DISCOVERY_SPEC_V1).

Computed from the SAME closed 15m/1h/4h windows the runtime hook sees
(nothing after the decision timestamp). They are NOT part of the runtime
feature schema (bot.ai.features), so no candidate using them is promotable:
exact runtime parity would first require adding them to the runtime schema.
"""
from __future__ import annotations

import math

RESEARCH_FEATURE_VERSION = "RESEARCH_FEATURES_V1"
RESEARCH_FEATURES = ("ema50_dist_atr", "h1_ema_align", "h4_trend_agree", "atr_pct_rank", "vol_z",
                     "range_compression", "dist_extreme_atr", "roc_16_dir", "cost_to_reward")


def _f(c, k):
    return float(c[k])


def _ema(xs, n):
    if not xs:
        return None
    a, e = 2.0 / (n + 1), xs[0]
    for x in xs[1:]:
        e = a * x + (1 - a) * e
    return e


def _atr(bars, n=14):
    if len(bars) < n + 1:
        return None
    trs = [max(_f(b, "h") - _f(b, "l"), abs(_f(b, "h") - _f(p, "c")), abs(_f(b, "l") - _f(p, "c")))
           for p, b in zip(bars[:-1], bars[1:])]
    return sum(trs[-n:]) / n


def compute(w15, w1h, w4h, *, direction: str, entry: float, stop: float, rr: float,
            cost_fraction: float) -> dict:
    s = 1.0 if str(direction).upper() == "LONG" else -1.0
    out = {k: None for k in RESEARCH_FEATURES}
    if len(w15) < 40:
        return out
    closes = [_f(b, "c") for b in w15]
    atr = _atr(w15)
    if atr and atr > 0:
        e50 = _ema(closes, 50)
        out["ema50_dist_atr"] = s * (closes[-1] - e50) / atr
        atrs = [_atr(w15[:k]) for k in range(16, len(w15) + 1)]
        atrs = [a for a in atrs if a]
        out["atr_pct_rank"] = sum(1 for a in atrs if a <= atr) / len(atrs) if atrs else None
        hi, lo = max(_f(b, "h") for b in w15), min(_f(b, "l") for b in w15)
        out["dist_extreme_atr"] = ((hi - closes[-1]) if s > 0 else (closes[-1] - lo)) / atr
    vols = [_f(b, "v") for b in w15 if b.get("v") is not None]
    if len(vols) >= 20:
        m = sum(vols[:-1]) / (len(vols) - 1)
        sd = math.sqrt(sum((v - m) ** 2 for v in vols[:-1]) / max(1, len(vols) - 2))
        out["vol_z"] = (vols[-1] - m) / sd if sd > 0 else 0.0
    r32 = max(_f(b, "h") for b in w15[-32:]) - min(_f(b, "l") for b in w15[-32:])
    rall = max(_f(b, "h") for b in w15) - min(_f(b, "l") for b in w15)
    out["range_compression"] = r32 / rall if rall > 0 else None
    if len(closes) > 16 and closes[-17] > 0:
        out["roc_16_dir"] = s * (closes[-1] / closes[-17] - 1.0)
    if len(w1h) >= 50:
        c1 = [_f(b, "c") for b in w1h]
        out["h1_ema_align"] = s * (1.0 if _ema(c1, 20) > _ema(c1, 50) else -1.0)
    if len(w4h) >= 7:
        c4 = [_f(b, "c") for b in w4h]
        out["h4_trend_agree"] = s * (1.0 if c4[-1] > c4[-7] else -1.0)
    risk = abs(float(entry) - float(stop)) / float(entry) if entry else None
    if risk and rr:
        out["cost_to_reward"] = float(cost_fraction) / (float(rr) * risk)
    return out
