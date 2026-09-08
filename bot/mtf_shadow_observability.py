"""Best-effort MTF shadow observability with zero decision effect.

This module owns the telemetry previously embedded in the runtime overlay.
``observe_result`` must always return the exact production result object it was
given. Shadow telemetry failures are logged and cannot alter strategy output,
score, thresholds, risk, release state, or exchange execution.
"""
from __future__ import annotations

from typing import Any

from bot import mtf_shadow
from bot.logger import log


def observe_result(
    symbol: str,
    k15,
    k1h,
    k4h,
    production_result: Any,
    *,
    min_score: float = 60,
    fee_mult: float = 2.0,
    vol_mult: float = 1.0,
):
    """Observe one canonical MTF result and return it unchanged by identity."""
    try:
        before = mtf_shadow.snapshot().get("unique_states", 0)
        mtf_shadow.observe(
            symbol,
            k15,
            k1h,
            k4h,
            production_result=production_result,
            min_score=min_score,
            fee_mult=fee_mult,
            vol_mult=vol_mult,
        )
        snap = mtf_shadow.snapshot()
        unique = snap.get("unique_states", 0)
        if unique != before and (unique == 1 or unique % 25 == 0):
            log.info(
                "[MTF_SHADOW] unique=%s eligible=%s survivors=%s "
                "nexus_approved=%s nexus_vetoed=%s "
                "decision_effect=NONE execution_effect=NONE",
                unique,
                snap.get("eligible_4h_dir_1h_neutral", 0),
                snap.get("shadow_pre_ai_survivors", 0),
                snap.get("shadow_nexus_approved", 0),
                snap.get("shadow_nexus_vetoed", 0),
            )
    except Exception as exc:
        log.debug(
            "[MTF_SHADOW] observability_failed error=%s "
            "decision_effect=NONE execution_effect=NONE",
            type(exc).__name__,
        )
    return production_result
