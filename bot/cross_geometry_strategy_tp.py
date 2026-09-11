"""Preserve the strategy TP when CROSS liquidation geometry tightens only the SL.

The CROSS risk hardening may need to move an ATR stop closer to entry so the
configured 50x leverage remains liquidation-compatible. Historically it also
compressed TP by the same fraction to preserve *gross* R:R. That changes the
strategy's original target and makes fixed round-trip costs dominate the much
smaller geometry, which can turn an otherwise valid NEXUS candidate into a
second-pass EV/R:R veto.

This overlay keeps the already-authoritative liquidation-safe SL but restores
only the original strategy TP. It does not manufacture a new target, lower any
NEXUS threshold, change leverage/sizing, or bypass the mandatory second NEXUS
validation. If the original TP is invalid, the original adjusted result is left
unchanged and downstream fail-closed gates remain authoritative.
"""
from __future__ import annotations

import math


def install(contract_risk_module, log) -> None:
    if getattr(contract_risk_module, "_strategy_tp_preservation_installed", False):
        return

    original_geometry = contract_risk_module._geometry_from_exact_mmr

    def geometry_preserving_strategy_tp(liquidation, sig, leverage: int) -> dict:
        result = original_geometry(liquidation, sig, leverage)
        if not isinstance(result, dict) or result.get("status") != "ADJUSTED":
            return result

        try:
            entry = float(sig.entry)
            original_tp = float(sig.tp)
            adjusted_sl = float(result["sl"])
            direction = str(sig.direction).upper()
        except (TypeError, ValueError, KeyError, AttributeError):
            return result

        if not all(math.isfinite(v) and v > 0 for v in (entry, original_tp, adjusted_sl)):
            return result
        if direction == "LONG" and not (adjusted_sl < entry < original_tp):
            return result
        if direction == "SHORT" and not (original_tp < entry < adjusted_sl):
            return result

        risk = abs(entry - adjusted_sl)
        reward = abs(original_tp - entry)
        if risk <= 0 or reward <= 0:
            return result

        updated = dict(result)
        compressed_tp = float(result.get("tp", original_tp))
        updated["tp"] = original_tp
        updated["rr"] = reward / risk
        updated["reason"] = "stop_compressed_strategy_tp_preserved"
        updated["compressed_tp_legacy"] = compressed_tp
        updated["strategy_tp_preserved"] = True
        log.warning(
            "[KUCOIN_CROSS_GEOMETRY_TP] symbol=%s direction=%s "
            "strategy_tp_preserved=true legacy_compressed_tp=%.10g strategy_tp=%.10g "
            "adjusted_gross_rr=%.3f leverage=%sx nexus_recheck_still_required=true "
            "thresholds_unchanged=true sizing_unchanged=true",
            getattr(sig, "symbol", "UNKNOWN"), direction, compressed_tp,
            original_tp, updated["rr"], int(leverage),
        )
        return updated

    contract_risk_module._geometry_from_exact_mmr = geometry_preserving_strategy_tp
    contract_risk_module._strategy_tp_preservation_installed = True
    log.warning(
        "[KUCOIN_CROSS_GEOMETRY_TP_GUARD] installed preserve_original_strategy_tp=true "
        "liquidation_safe_sl_unchanged=true nexus_recheck_unchanged=true "
        "rr_threshold_unchanged=true leverage_unchanged=true sizing_unchanged=true"
    )
