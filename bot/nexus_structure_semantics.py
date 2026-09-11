"""Conservative NEXUS market-structure semantics.

The canonical STRUCTURE model correctly gives strong credit to directional
UPTREND/DOWNTREND structure, but every non-directional state currently collapses
to a zero score. That makes neutral RANGING structure indistinguishable from an
explicitly opposite structure, even when higher-timeframe direction and regime
are aligned.

This hardening preserves zero for adverse/conflicting structure and only grants
limited partial credit to neutral structure when BOTH the NEXUS regime and MTF
context already support the proposed direction. It does not change thresholds,
risk limits, leverage, sizing, exchange permissions, or any downstream gate.
"""
from __future__ import annotations

from typing import Any


def _value(value: Any) -> str:
    return str(getattr(value, "value", value)).upper()


def _structure_model(models):
    return next((m for m in (models or []) if getattr(m, "name", "") == "STRUCTURE"), None)


def _regime_supports(direction, regime) -> bool:
    d = _value(direction)
    r = _value(regime)
    if d == "LONG":
        return r in {"TRENDING_BULL", "BREAKOUT", "ACCUMULATION"}
    if d == "SHORT":
        return r in {"TRENDING_BEAR", "BREAKDOWN", "DISTRIBUTION"}
    return False


def _mtf_supports(direction, mtf: dict) -> bool:
    if not isinstance(mtf, dict):
        return False
    mtf_dir = _value(mtf.get("direction", "WAIT"))
    try:
        mtf_score = float(mtf.get("score", 0.0) or 0.0)
    except (TypeError, ValueError):
        mtf_score = 0.0
    return mtf_dir == _value(direction) and mtf_score >= 70.0


def contextual_structure_score(models, mtf: dict, direction, regime) -> tuple[float, str]:
    """Return partial neutral-structure credit without masking adverse evidence.

    Rules are intentionally conservative:
    - aligned directional STRUCTURE is left to the canonical scorer;
    - opposite directional STRUCTURE remains zero;
    - CHoCH or opposite BOS remains zero;
    - neutral credit is possible only with aligned regime + MTF >= 70;
    - aligned BOS from a neutral state gets moderate, not full, credit;
    - RANGING gets small neutral credit;
    - ACCUMULATION only helps LONG and DISTRIBUTION only helps SHORT.
    """
    model = _structure_model(models)
    if model is None or not bool(getattr(model, "available", False)):
        return 0.0, "UNAVAILABLE"

    model_dir = _value(getattr(model, "direction", "WAIT"))
    target = _value(direction)
    if model_dir == target:
        return float(getattr(model, "confidence", 0.0) or 0.0), "ALIGNED_DIRECTIONAL"
    if model_dir not in {"WAIT", "NONE", ""}:
        return 0.0, "OPPOSITE_DIRECTIONAL"

    details = dict(getattr(model, "details", {}) or {})
    state = str(details.get("structure", "UNKNOWN")).upper()
    bos = bool(details.get("bos", False))
    bos_dir = str(details.get("bos_dir", "NONE")).upper()
    choch = bool(details.get("choch", False))

    if choch:
        return 0.0, f"{state}_CHOCH"

    if bos:
        bos_target = "LONG" if bos_dir in {"LONG", "BULLISH"} else (
            "SHORT" if bos_dir in {"SHORT", "BEARISH"} else "WAIT"
        )
        if bos_target != target:
            return 0.0, f"{state}_BOS_OPPOSITE"

    if not _regime_supports(direction, regime) or not _mtf_supports(direction, mtf):
        return 0.0, f"{state}_NO_HTF_SUPPORT"

    if bos:
        return 55.0, f"{state}_ALIGNED_BOS"

    if state == "RANGING":
        return 25.0, "RANGING_NEUTRAL"
    if state == "ACCUMULATION" and target == "LONG":
        return 35.0, "ACCUMULATION_LONG"
    if state == "DISTRIBUTION" and target == "SHORT":
        return 35.0, "DISTRIBUTION_SHORT"

    # Opposite/ambiguous states are not rewarded.
    return 0.0, f"{state}_NO_CREDIT"


def _recompute(sc: dict, weights: dict) -> dict:
    components = dict(sc.get("components") or {})
    unavailable = set(sc.get("unavailable") or [])
    neutral = set(sc.get("neutral") or [])
    excluded = unavailable | neutral
    active = {
        key: float(value)
        for key, value in components.items()
        if key in weights and key not in excluded
    }
    weight_total = sum(float(weights[key]) for key in active)
    if weight_total <= 0:
        return sc
    total = sum(
        active[key] * (float(weights[key]) / weight_total)
        for key in active
    )
    out = dict(sc)
    out["weight_used"] = round(weight_total, 3)
    out["total"] = round(total, 2)
    return out


def install(nexus_ai, log) -> None:
    if getattr(nexus_ai, "_structure_semantics_installed", False):
        return

    original = nexus_ai._score_components

    def score_components_structure_aware(models, mtf, rr_net, direction, regime):
        sc = original(models, mtf, rr_net, direction, regime)
        if not isinstance(sc, dict):
            return sc

        components = dict(sc.get("components") or {})
        canonical = float(components.get("MARKET_STRUCTURE", 0.0) or 0.0)
        contextual, reason = contextual_structure_score(models, mtf, direction, regime)

        # Never reduce a canonical aligned score and never turn an opposite
        # directional structure into credit. Context only separates neutral
        # structure from truly adverse structure.
        adjusted = max(canonical, contextual)
        if adjusted <= canonical:
            out = dict(sc)
            out["structure_context"] = reason
            return out

        components["MARKET_STRUCTURE"] = round(adjusted, 1)
        out = dict(sc)
        out["components"] = components
        out["structure_context"] = reason
        return _recompute(out, nexus_ai.WEIGHTS)

    nexus_ai._score_components = score_components_structure_aware
    nexus_ai._structure_semantics_installed = True
    log.warning(
        "[NEXUS_STRUCTURE_SEMANTICS] installed neutral_structure_partial_credit=true "
        "requires_regime_and_mtf_alignment=true ranging=25 favorable_accum_dist=35 "
        "aligned_neutral_bos=55 choch_or_opposite=0 threshold_unchanged=true "
        "execution_effect=SCORING_ONLY"
    )
