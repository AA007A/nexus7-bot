"""Strict consistency guard for NEXUS HTF-regime transitions.

The canonical NEXUS regime classifier uses confirmed 4H ADX/DI, while its MTF
classifier uses EMA structure on confirmed 4H/1H/15m candles. During a real
trend transition those two valid measurements can temporarily disagree. The
legacy regime gate treated every disagreement as an absolute counter-trend veto
(compatibility=25), even when all three confirmed MTFs had already aligned in
the candidate direction.

This module does *not* flip the regime and does *not* lower any score/R:R/risk
threshold. It only changes the compatibility value from hard-veto 25 to the
existing UNKNOWN-like value 35 under a deliberately narrow transition cohort:
- regime is TRENDING_BULL vs candidate SHORT, or TRENDING_BEAR vs candidate LONG;
- confirmed 4H, 1H and 15m EMA biases are all exactly aligned with candidate;
- MTF score is 100 with no conflict;
- opposite ADX trend strength is transitional (25 < ADX <= 35).

Everything else retains the original regime compatibility unchanged.
"""
from __future__ import annotations

import contextvars
import functools
from typing import Any


_MTF_CONTEXT: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "bgx_nexus_mtf_transition_context", default=None
)
_REGIME_CONTEXT: contextvars.ContextVar[tuple[Any, dict] | None] = contextvars.ContextVar(
    "bgx_nexus_regime_transition_context", default=None
)


def _direction_value(value: Any) -> str:
    return str(getattr(value, "value", value) or "").upper()


def _strict_three_tf_alignment(mtf: dict | None, direction: Any) -> bool:
    if not isinstance(mtf, dict):
        return False
    if bool(mtf.get("conflict")) or not bool(mtf.get("available")):
        return False
    if float(mtf.get("score", 0.0) or 0.0) != 100.0:
        return False
    wanted = _direction_value(direction)
    if wanted not in ("LONG", "SHORT"):
        return False
    if _direction_value(mtf.get("direction")) != wanted:
        return False
    detail = str(mtf.get("detail", "")).upper()
    return all(f"{tf}={wanted}" in detail for tf in ("4H", "1H", "15M"))


def _transition_allowed(regime: Any, direction: Any, mtf: dict | None, details: dict | None) -> bool:
    regime_v = _direction_value(regime)
    direction_v = _direction_value(direction)
    opposite = (
        regime_v == "TRENDING_BULL" and direction_v == "SHORT"
    ) or (
        regime_v == "TRENDING_BEAR" and direction_v == "LONG"
    )
    if not opposite or not _strict_three_tf_alignment(mtf, direction):
        return False
    try:
        adx = float((details or {}).get("adx", 0.0) or 0.0)
    except (TypeError, ValueError):
        return False
    # At >35 the opposite established ADX trend remains a hard veto. At <=25
    # detect_regime would not classify the market as TRENDING_* in the first place.
    return 25.0 < adx <= 35.0


def install(nexus_ai, log) -> None:
    if getattr(nexus_ai, "_regime_transition_consistency_installed", False):
        return

    original_analyze_mtf = nexus_ai.analyze_mtf
    original_detect_regime = nexus_ai.detect_regime
    original_compatibility = nexus_ai.regime_compatibility

    @functools.wraps(original_analyze_mtf)
    def analyze_mtf_tracked(*args, **kwargs):
        result = original_analyze_mtf(*args, **kwargs)
        _MTF_CONTEXT.set(result if isinstance(result, dict) else None)
        return result

    @functools.wraps(original_detect_regime)
    def detect_regime_tracked(*args, **kwargs):
        result = original_detect_regime(*args, **kwargs)
        if isinstance(result, tuple) and len(result) == 2:
            regime, details = result
            _REGIME_CONTEXT.set((regime, details if isinstance(details, dict) else {}))
        else:
            _REGIME_CONTEXT.set(None)
        return result

    @functools.wraps(original_compatibility)
    def regime_compatibility_transition(regime, direction):
        base = float(original_compatibility(regime, direction))
        mtf = _MTF_CONTEXT.get()
        observed = _REGIME_CONTEXT.get()
        details = observed[1] if observed and observed[0] == regime else {}
        if base < 30.0 and _transition_allowed(regime, direction, mtf, details):
            log.warning(
                "[NEXUS_REGIME_TRANSITION] regime=%s direction=%s base_compat=%.1f "
                "transition_compat=35.0 adx=%.2f mtf=%s "
                "thresholds_unchanged=true leverage_unchanged=true",
                _direction_value(regime), _direction_value(direction), base,
                float(details.get("adx", 0.0) or 0.0),
                str(mtf.get("detail", "")) if isinstance(mtf, dict) else "",
            )
            return 35.0
        return base

    nexus_ai.analyze_mtf = analyze_mtf_tracked
    nexus_ai.detect_regime = detect_regime_tracked
    nexus_ai.regime_compatibility = regime_compatibility_transition
    nexus_ai._regime_transition_consistency_installed = True

    log.warning(
        "[NEXUS_REGIME_TRANSITION_GUARD] installed strict_three_tf_alignment=true "
        "adx_transition_min_exclusive=25 adx_transition_max_inclusive=35 "
        "hard_opposite_trend_above_35=true score_threshold_unchanged=true "
        "rr_threshold_unchanged=true leverage_unchanged=true"
    )
