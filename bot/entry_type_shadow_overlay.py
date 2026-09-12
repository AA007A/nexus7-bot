"""Passive Analyzer overlay for entry-type diagnostics.

Runs only after the fully composed analyzer has already returned HOLD. It
reconstructs the same closed-candle 15m view, asks the live detector for the
current entry type, and, for PULLBACK only, evaluates multi-bar expansion in
shadow. The original analyzer return value is always preserved.
"""
from __future__ import annotations

import numpy as np

from bot import entry_type_shadow


def install(Analyzer, strategy, log) -> None:
    if getattr(Analyzer, "_entry_type_shadow_installed", False):
        return

    original = Analyzer.analyze_mtf

    def analyze_with_entry_shadow(self, symbol, k15, k1h, k4h,
                                  min_score=60, fee_mult=2.0, vol_mult=1.0):
        result = original(
            self, symbol, k15, k1h, k4h,
            min_score=min_score, fee_mult=fee_mult, vol_mult=vol_mult,
        )
        if result is not None:
            return result
        try:
            if len(k4h) < 10 or len(k15) < 20:
                return result

            def ga(kl):
                return (
                    [float(k["c"]) for k in kl],
                    [float(k["h"]) for k in kl],
                    [float(k["l"]) for k in kl],
                    [float(k["o"]) for k in kl],
                    [k["v"] for k in kl],
                )

            c4h, h4h, l4h, _, _ = ga(k4h[:-1] if len(k4h) > 10 else k4h)
            c15, h15, l15, o15, v15 = ga(k15[:-1] if len(k15) > 20 else k15)
            a4 = strategy.atr(h4h, l4h, c4h)
            atr_4h = float(a4[-1])
            regime = strategy.detect_regime(c4h, h4h, l4h, atr_4h)
            if regime not in ("TRENDING_UP", "TRENDING_DOWN"):
                return result
            direction = "LONG" if regime == "TRENDING_UP" else "SHORT"

            a15 = strategy.atr(h15, l15, c15)
            atr_15 = float(a15[-1])
            entry_ok, current_type = strategy.detect_entry(
                c15, h15, l15, o15, v15, direction, atr_15
            )
            if not entry_ok or current_type != "PULLBACK":
                return result

            shadow = entry_type_shadow.assess(
                closes=c15, highs=h15, lows=l15, opens=o15,
                direction=direction, atr_v=atr_15, current_type=current_type,
            )
            log.info(
                "[ENTRY_TYPE_SHADOW] symbol=%s side=%s current=%s shadow=%s "
                "would_reclassify=%s body=%.2fATR net2=%.2fATR net3=%.2fATR "
                "directional_bars=%d/3 close_location=%.2f failed_reasons=%s "
                "live_result=HOLD shadow_only=true entry_type_unchanged=true "
                "nexus_called_by_shadow=false signal_created_by_shadow=false "
                "thresholds_unchanged=true leverage_unchanged=true "
                "decision_effect=NONE execution_effect=NONE",
                symbol, direction, current_type, shadow.get("shadow_type", current_type),
                str(bool(shadow.get("would_reclassify"))).lower(),
                float(shadow.get("body_atr", 0.0)), float(shadow.get("net_2bar_atr", 0.0)),
                float(shadow.get("net_3bar_atr", 0.0)), int(shadow.get("directional_bars", 0)),
                float(shadow.get("close_location", 0.0)),
                ",".join(shadow.get("failed_reasons") or []) or "NONE",
            )
        except Exception as exc:
            log.debug("[ENTRY_TYPE_SHADOW] symbol=%s diagnostic_error=%s", symbol, type(exc).__name__)
        return result

    Analyzer.analyze_mtf = analyze_with_entry_shadow
    Analyzer._entry_type_shadow_installed = True
    log.info(
        "[ENTRY_TYPE_SHADOW] installed multi_bar_displacement=true closed_candles=true "
        "shadow_only=true entry_type_unchanged=true thresholds_unchanged=true "
        "leverage_unchanged=true decision_effect=NONE execution_effect=NONE"
    )
