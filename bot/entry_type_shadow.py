"""Read-only diagnostics for possible false PULLBACK classifications.

The live detector classifies BOS_BREAK first, then single-candle MOMENTUM, then a
very broad 20-bar PULLBACK fallback. This module measures whether a live
PULLBACK actually contains coherent multi-bar directional displacement. It never
changes entry_type, creates a Signal, calls NEXUS, or affects execution.
"""
from __future__ import annotations

import threading
from collections import Counter
from typing import Any

_LOCK = threading.Lock()
_SAMPLES = 0
_RECLASSIFIED = 0
_REASONS: Counter[str] = Counter()


def assess(*, closes, highs, lows, opens, direction: str, atr_v: float,
           current_type: str) -> dict[str, Any]:
    c = [float(x) for x in closes]
    h = [float(x) for x in highs]
    l = [float(x) for x in lows]
    o = [float(x) for x in opens]
    atr_v = float(atr_v or 0.0)
    if current_type != "PULLBACK" or direction not in ("LONG", "SHORT") or atr_v <= 0 or len(c) < 4:
        return {"eligible": False, "shadow_type": current_type, "would_reclassify": False}

    sign = 1.0 if direction == "LONG" else -1.0
    body_atr = abs(c[-1] - o[-1]) / atr_v
    net_2bar_atr = sign * (c[-1] - c[-3]) / atr_v
    net_3bar_atr = sign * (c[-1] - c[-4]) / atr_v
    directional_bars = sum(
        1 for i in range(-3, 0)
        if ((c[i] > o[i]) if direction == "LONG" else (c[i] < o[i]))
    )
    candle_range = max(0.0, h[-1] - l[-1])
    if candle_range > 0:
        close_location = ((c[-1] - l[-1]) / candle_range) if direction == "LONG" else ((h[-1] - c[-1]) / candle_range)
    else:
        close_location = 0.0

    # Multi-bar expansion: do not reuse volume/ADX because those are independent
    # downstream gates. Require coherent displacement, persistence, and a close
    # in the directional side of the candle. This is intentionally stricter
    # than merely saying the 20-bar range is non-trivial.
    coherent = directional_bars >= 2
    displaced = net_3bar_atr >= 0.45 and net_2bar_atr >= 0.20
    close_quality = close_location >= 0.60
    would_reclassify = coherent and displaced and close_quality

    reasons = []
    if not coherent:
        reasons.append("DIRECTIONAL_PERSISTENCE")
    if not displaced:
        reasons.append("MULTIBAR_DISPLACEMENT")
    if not close_quality:
        reasons.append("CLOSE_LOCATION")

    return {
        "eligible": True,
        "shadow_type": "MOMENTUM_MULTI_BAR" if would_reclassify else "PULLBACK",
        "would_reclassify": would_reclassify,
        "body_atr": round(body_atr, 4),
        "net_2bar_atr": round(net_2bar_atr, 4),
        "net_3bar_atr": round(net_3bar_atr, 4),
        "directional_bars": directional_bars,
        "close_location": round(close_location, 4),
        "failed_reasons": reasons,
    }


def observe(*, symbol: str, closes, highs, lows, opens, direction: str,
            atr_v: float, current_type: str, strict_failures: list[str], log) -> None:
    global _SAMPLES, _RECLASSIFIED
    result = assess(
        closes=closes, highs=highs, lows=lows, opens=opens,
        direction=direction, atr_v=atr_v, current_type=current_type,
    )
    if not result.get("eligible"):
        return

    remaining = list(strict_failures or [])
    if result["would_reclassify"]:
        remaining = [x for x in remaining if x != "ENTRY_TYPE"]

    with _LOCK:
        _SAMPLES += 1
        if result["would_reclassify"]:
            _RECLASSIFIED += 1
        for reason in result.get("failed_reasons") or []:
            _REASONS[reason] += 1
        samples = _SAMPLES
        reclassified = _RECLASSIFIED
        reasons = Counter(_REASONS)

    log.info(
        "[ENTRY_TYPE_SHADOW] symbol=%s side=%s current=%s shadow=%s "
        "would_reclassify=%s body=%.2fATR net2=%.2fATR net3=%.2fATR "
        "directional_bars=%d/3 close_location=%.2f remaining_blockers=%s "
        "shadow_only=true entry_type_unchanged=true nexus_called=false "
        "signal_created=false thresholds_unchanged=true leverage_unchanged=true "
        "decision_effect=NONE execution_effect=NONE",
        symbol, direction, current_type, result["shadow_type"],
        str(bool(result["would_reclassify"])).lower(),
        result["body_atr"], result["net_2bar_atr"], result["net_3bar_atr"],
        result["directional_bars"], result["close_location"],
        ",".join(remaining) or "NONE",
    )
    if samples % 6 == 0:
        top = ",".join(f"{k}:{v}" for k, v in reasons.most_common(3)) or "NONE"
        log.info(
            "[ENTRY_TYPE_SHADOW_SUMMARY] samples=%d reclassified=%d rate=%.1f%% "
            "top_nonexpansion_reasons=%s shadow_only=true entry_type_unchanged=true "
            "decision_effect=NONE execution_effect=NONE",
            samples, reclassified, 100.0 * reclassified / samples, top,
        )
