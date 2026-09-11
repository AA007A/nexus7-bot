"""NEXUS optional-evidence semantics.

Optional enrichment must not be treated as adverse evidence merely because a
provider is absent or returns no directional conviction. Core market-data
integrity remains fail-closed.

This runtime hardening does two narrowly-scoped things:

1. ``validate_data`` still reports missing ticker/funding/OI/order-book fields,
   but removes the *global* data-quality deduction for those optional fields.
   Their availability is already represented by the corresponding model or
   score component. Core candle quantity/freshness/OHLC penalties are preserved.
2. A DERIVATIVES model that is available but returns WAIT with zero confidence
   is treated as non-directional evidence and excluded from the weighted score,
   with the remaining component weights renormalized. A derivatives model that
   points against the proposed trade remains active and therefore still
   penalizes the setup; aligned derivatives still contribute normally.

No threshold, leverage, sizing, risk limit, news gate, drawdown gate or exchange
execution permission is changed here.
"""
from __future__ import annotations

from typing import Any


_OPTIONAL_PENALTIES = {
    "ticker": 4.0,
    "funding": 3.0,
    "open_interest": 3.0,
    "orderbook": 2.0,
}


def _direction_value(value: Any) -> str:
    return str(getattr(value, "value", value)).upper()


def _optional_missing_penalty(*, ticker=None, funding=None, oi=None,
                              orderbook=None) -> float:
    """Mirror the legacy optional DQ deduction so only that deduction is undone."""
    penalty = 0.0
    if ticker is None or not getattr(ticker, "get", lambda *_: None)("lastPrice"):
        penalty += _OPTIONAL_PENALTIES["ticker"]
    if funding is None:
        penalty += _OPTIONAL_PENALTIES["funding"]
    if oi is None:
        penalty += _OPTIONAL_PENALTIES["open_interest"]
    if orderbook is None or not getattr(orderbook, "get", lambda *_: None)("b"):
        penalty += _OPTIONAL_PENALTIES["orderbook"]
    return min(12.0, penalty)


def _derivatives_is_nondirectional(models) -> bool:
    """True only when DERIVATIVES exists but produced no directional conviction."""
    model = next((m for m in (models or []) if getattr(m, "name", "") == "DERIVATIVES"), None)
    if model is None or not bool(getattr(model, "available", False)):
        return False
    return (
        _direction_value(getattr(model, "direction", "WAIT")) == "WAIT"
        and float(getattr(model, "confidence", 0.0) or 0.0) <= 0.0
    )


def _renormalize_without_neutral_derivatives(sc: dict, weights: dict,
                                               models) -> dict:
    """Exclude only non-directional optional derivatives from the score denominator."""
    if not isinstance(sc, dict) or not _derivatives_is_nondirectional(models):
        return sc

    components = dict(sc.get("components") or {})
    unavailable = set(sc.get("unavailable") or [])
    if "DERIVATIVES" in unavailable:
        return sc

    neutral = set(sc.get("neutral") or [])
    neutral.add("DERIVATIVES")
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
    out["neutral"] = sorted(neutral)
    out["weight_used"] = round(weight_total, 3)
    out["total"] = round(total, 2)
    return out


def install(nexus_ai, log) -> None:
    if getattr(nexus_ai, "_optional_evidence_semantics_installed", False):
        return

    original_validate = nexus_ai.validate_data
    original_score_components = nexus_ai._score_components

    def validate_data_optional_aware(symbol, k15, k1h, k4h, ticker=None,
                                     funding=None, oi=None, orderbook=None):
        dq = original_validate(
            symbol, k15, k1h, k4h,
            ticker=ticker, funding=funding, oi=oi, orderbook=orderbook,
        )
        # The legacy validator already appended these fields to ``unavailable``.
        # Restore only the known optional deduction; every core-data penalty
        # remains untouched and can still make ``is_acceptable`` fail closed.
        restore = _optional_missing_penalty(
            ticker=ticker, funding=funding, oi=oi, orderbook=orderbook,
        )
        dq.score = min(100.0, float(getattr(dq, "score", 0.0)) + restore)
        setattr(dq, "optional_unavailable_penalty_removed", round(restore, 2))
        return dq

    def score_components_optional_aware(models, mtf, rr_net, direction, regime):
        sc = original_score_components(models, mtf, rr_net, direction, regime)
        return _renormalize_without_neutral_derivatives(sc, nexus_ai.WEIGHTS, models)

    nexus_ai.validate_data = validate_data_optional_aware
    nexus_ai._score_components = score_components_optional_aware
    nexus_ai._optional_evidence_semantics_installed = True
    log.warning(
        "[NEXUS_OPTIONAL_EVIDENCE] installed optional_missing_global_dq_penalty=0 "
        "neutral_derivatives=EXCLUDE_AND_RENORMALIZE "
        "opposing_derivatives=RETAINED aligned_derivatives=RETAINED "
        "core_data_fail_closed=true threshold_unchanged=true execution_effect=SCORING_ONLY"
    )
