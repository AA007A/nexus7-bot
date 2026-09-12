"""Read-only calibration for Adaptive MTF HOLD decisions.

This module never creates Signals, calls NEXUS, touches the exchange, or mutates
strategy thresholds. It records one compact reject snapshot per symbol and
confirmed 15m candle so we can distinguish a single near-threshold miss from a
setup that fails several adaptive requirements simultaneously.

It also attributes raw gate failures to independent evidence families. This is
important because score_tf already embeds trend/alignment, ADX, volume,
momentum, volatility and structure, while Adaptive can hard-check some of the
same evidence again and NEXUS later scores many of those dimensions a third
time. Attribution is observability only; it has zero decision/execution effect.
"""
from __future__ import annotations

import threading
from collections import Counter, deque
from typing import Any

_LOCK = threading.Lock()
_SEEN: set[tuple[str, object]] = set()
_SEEN_ORDER = deque(maxlen=5000)

_AGG_TS: object | None = None
_AGG_FAILURES: Counter[str] = Counter()
_AGG_FAMILIES: Counter[str] = Counter()
_AGG_OVERLAPS: Counter[str] = Counter()
_AGG_SAMPLES = 0
_AGG_NEAR_MISS_1 = 0
_AGG_NEAR_MISS_2 = 0
_AGG_INDEPENDENT_NEAR_1 = 0
_AGG_INDEPENDENT_NEAR_2 = 0
_AGG_EMITTED = False
_AGG_MIN_SAMPLES = 6

# Raw Adaptive failures collapsed into economically/technically distinct
# evidence families. Four timeframe score failures are one correlated score
# family, not four independent pieces of evidence.
_FAILURE_FAMILY = {
    "SCORE_4H": "SCORE_FAMILY",
    "SCORE_1H": "SCORE_FAMILY",
    "SCORE_15M": "SCORE_FAMILY",
    "SCORE_COMBINED": "SCORE_FAMILY",
    "ALIGN_15M": "TREND_ALIGNMENT",
    "CANONICAL_ALIGNMENT": "TREND_ALIGNMENT",
    "HTF_OPPOSITION": "HTF_OPPOSITION",
    "ADX": "TREND_STRENGTH",
    "VOLUME": "ACTIVITY_VOLUME",
    "ENTRY_TYPE": "ENTRY_TRIGGER",
    "RSI_LONG": "TAIL_MOMENTUM_GUARD",
    "RSI_SHORT": "TAIL_MOMENTUM_GUARD",
    "EXTENSION": "ANTI_CHASE",
}

# Which hard Adaptive checks reuse evidence that is already embedded in
# strategy.score_tf. This does not say the check is wrong; it quantifies that it
# is not statistically independent from the score gate.
_SCORE_TF_OVERLAP = {
    "ALIGN_15M": "score_tf.trend_s(EMA/aligned)",
    "CANONICAL_ALIGNMENT": "score_tf.trend_s(EMA/aligned)",
    "ADX": "score_tf.trend_s(ADX)",
    "VOLUME": "score_tf.vol_s(vol_r)",
    "RSI_LONG": "score_tf.momentum_s(RSI)",
    "RSI_SHORT": "score_tf.momentum_s(RSI)",
}

# Dimensions that NEXUS scores again downstream. This makes the potential
# triple-count explicit without changing NEXUS behavior.
_NEXUS_REEVALUATES = {
    "SCORE_FAMILY": "TREND_ALIGNMENT/MOMENTUM/VOLUME/MARKET_STRUCTURE/VOLATILITY/MULTI_TIMEFRAME",
    "TREND_ALIGNMENT": "TREND_ALIGNMENT/MULTI_TIMEFRAME",
    "TREND_STRENGTH": "TREND_ALIGNMENT/regime",
    "ACTIVITY_VOLUME": "VOLUME",
    "TAIL_MOMENTUM_GUARD": "MOMENTUM",
    "ENTRY_TRIGGER": "MARKET_STRUCTURE/MOMENTUM",
}


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
        return out if out == out and abs(out) != float("inf") else default
    except (TypeError, ValueError, OverflowError):
        return default


def _closed_15m_ts(k15) -> object:
    data = list(k15 or [])
    if not data:
        return "missing"
    bar = data[-2] if len(data) > 20 else data[-1]
    return bar.get("ts") or bar.get("time") or bar.get("timestamp") or len(data)


def _remember(symbol: str, k15) -> bool:
    key = (str(symbol), _closed_15m_ts(k15))
    with _LOCK:
        if key in _SEEN:
            return False
        if len(_SEEN_ORDER) == _SEEN_ORDER.maxlen:
            old = _SEEN_ORDER.popleft()
            _SEEN.discard(old)
        _SEEN.add(key)
        _SEEN_ORDER.append(key)
        return True


def _format_counter(counter: Counter[str], limit: int = 6) -> str:
    if not counter:
        return "NONE"
    return ",".join(f"{name}:{count}" for name, count in counter.most_common(limit))


def evidence_attribution(failures: list[str]) -> dict[str, Any]:
    """Collapse correlated failures and identify repeated evidence checks."""
    families: list[str] = []
    seen: set[str] = set()
    overlaps: list[str] = []
    nexus_rechecks: list[str] = []

    for failure in failures:
        family = _FAILURE_FAMILY.get(failure, failure)
        if family not in seen:
            seen.add(family)
            families.append(family)
        if failure in _SCORE_TF_OVERLAP:
            overlaps.append(failure)

    for family in families:
        if family in _NEXUS_REEVALUATES:
            nexus_rechecks.append(family)

    raw_count = len(failures)
    independent_count = len(families)
    return {
        "families": families,
        "independent_count": independent_count,
        "raw_count": raw_count,
        "collapsed_duplicates": max(0, raw_count - independent_count),
        "score_tf_overlap_checks": overlaps,
        "score_tf_overlap_count": len(overlaps),
        "nexus_recheck_families": nexus_rechecks,
        "nexus_recheck_count": len(nexus_rechecks),
    }


def _component_deficits(score: dict) -> dict[str, float]:
    """Normalize score_tf component deficits against their documented maxima."""
    maxima = {
        "trend": ("trend_s", 30.0),
        "volume": ("vol_s", 20.0),
        "momentum": ("momentum_s", 20.0),
        "volatility": ("atr_s", 15.0),
        "structure": ("struct_s", 15.0),
    }
    out: dict[str, float] = {}
    for name, (key, maximum) in maxima.items():
        value = max(0.0, min(maximum, _finite(score.get(key))))
        out[name] = round(maximum - value, 2)
    return out


def _emit_aggregate(log, *, candle_ts: object, samples: int,
                    failures: Counter[str], families: Counter[str],
                    overlaps: Counter[str], near1: int, near2: int,
                    independent_near1: int, independent_near2: int,
                    trigger: str) -> None:
    if samples <= 0:
        return
    multi = max(0, samples - near2)
    independent_multi = max(0, samples - independent_near2)
    log.info(
        "[ADAPTIVE_MTF_FUNNEL_SUMMARY] candle=%s samples=%d "
        "near_miss_1=%d near_miss_2=%d multi_gate_gt2=%d "
        "independent_near_1=%d independent_near_2=%d independent_multi_gt2=%d "
        "top_failures=%s top_families=%s score_tf_overlaps=%s trigger=%s "
        "thresholds_unchanged=true leverage_unchanged=true "
        "decision_effect=NONE execution_effect=NONE",
        candle_ts, samples, near1, near2, multi,
        independent_near1, independent_near2, independent_multi,
        _format_counter(failures), _format_counter(families),
        _format_counter(overlaps), trigger,
    )


def _aggregate_snapshot(*, candle_ts: object, snapshot: dict[str, Any], log) -> None:
    global _AGG_TS, _AGG_FAILURES, _AGG_FAMILIES, _AGG_OVERLAPS, _AGG_SAMPLES
    global _AGG_NEAR_MISS_1, _AGG_NEAR_MISS_2
    global _AGG_INDEPENDENT_NEAR_1, _AGG_INDEPENDENT_NEAR_2, _AGG_EMITTED

    with _LOCK:
        if _AGG_TS is not None and candle_ts != _AGG_TS:
            previous = (
                _AGG_TS, _AGG_SAMPLES, Counter(_AGG_FAILURES),
                Counter(_AGG_FAMILIES), Counter(_AGG_OVERLAPS),
                _AGG_NEAR_MISS_1, _AGG_NEAR_MISS_2,
                _AGG_INDEPENDENT_NEAR_1, _AGG_INDEPENDENT_NEAR_2,
                _AGG_EMITTED,
            )
            _AGG_TS = candle_ts
            _AGG_FAILURES = Counter()
            _AGG_FAMILIES = Counter()
            _AGG_OVERLAPS = Counter()
            _AGG_SAMPLES = 0
            _AGG_NEAR_MISS_1 = 0
            _AGG_NEAR_MISS_2 = 0
            _AGG_INDEPENDENT_NEAR_1 = 0
            _AGG_INDEPENDENT_NEAR_2 = 0
            _AGG_EMITTED = False
        else:
            previous = None
            if _AGG_TS is None:
                _AGG_TS = candle_ts

        failures_list = list(snapshot.get("failures") or [])
        raw_count = int(snapshot.get("failure_count") or len(failures_list))
        attribution = snapshot.get("attribution") or evidence_attribution(failures_list)
        families_list = list(attribution.get("families") or [])
        independent_count = int(attribution.get("independent_count") or len(families_list))
        overlap_list = list(attribution.get("score_tf_overlap_checks") or [])

        _AGG_FAILURES.update(failures_list)
        _AGG_FAMILIES.update(families_list)
        _AGG_OVERLAPS.update(overlap_list)
        _AGG_SAMPLES += 1
        if raw_count <= 1:
            _AGG_NEAR_MISS_1 += 1
        if raw_count <= 2:
            _AGG_NEAR_MISS_2 += 1
        if independent_count <= 1:
            _AGG_INDEPENDENT_NEAR_1 += 1
        if independent_count <= 2:
            _AGG_INDEPENDENT_NEAR_2 += 1

        immediate = None
        if _AGG_SAMPLES >= _AGG_MIN_SAMPLES and not _AGG_EMITTED:
            _AGG_EMITTED = True
            immediate = (
                _AGG_TS, _AGG_SAMPLES, Counter(_AGG_FAILURES),
                Counter(_AGG_FAMILIES), Counter(_AGG_OVERLAPS),
                _AGG_NEAR_MISS_1, _AGG_NEAR_MISS_2,
                _AGG_INDEPENDENT_NEAR_1, _AGG_INDEPENDENT_NEAR_2,
            )

    if previous is not None:
        (pts, psamples, pfailures, pfamilies, poverlaps, pnear1, pnear2,
         pind1, pind2, pemitted) = previous
        if not pemitted:
            _emit_aggregate(
                log, candle_ts=pts, samples=psamples, failures=pfailures,
                families=pfamilies, overlaps=poverlaps,
                near1=pnear1, near2=pnear2,
                independent_near1=pind1, independent_near2=pind2,
                trigger="candle_rollover",
            )
    if immediate is not None:
        (its, isamples, ifailures, ifamilies, ioverlaps, inear1, inear2,
         iind1, iind2) = immediate
        _emit_aggregate(
            log, candle_ts=its, samples=isamples, failures=ifailures,
            families=ifamilies, overlaps=ioverlaps,
            near1=inear1, near2=inear2,
            independent_near1=iind1, independent_near2=iind2,
            trigger="sample_threshold",
        )


def failure_vector(*, direction: str, bull_4h: bool, bear_4h: bool,
                   bull_1h: bool, bear_1h: bool, s4h: dict, s1h: dict,
                   s15: dict, combined: float, entry_type: str,
                   extension_atr: float, thresholds: dict) -> dict[str, Any]:
    min_4h = _finite(thresholds.get("min_4h"))
    min_1h = _finite(thresholds.get("min_1h"))
    min_15m = _finite(thresholds.get("min_15m"))
    min_combined = _finite(thresholds.get("min_combined"))
    min_vol = _finite(thresholds.get("min_vol"))
    min_adx = _finite(thresholds.get("min_adx"))
    max_extension = _finite(thresholds.get("max_extension_atr"))

    score_4h = _finite(s4h.get("total"))
    score_1h = _finite(s1h.get("total"))
    score_15m = _finite(s15.get("total"))
    combined_score = _finite(combined)
    vol = _finite(s15.get("vol_r"))
    adx = _finite(s15.get("adx_v"))
    rsi = _finite(s15.get("rsi_v"), 50.0)
    extension = _finite(extension_atr, 999.0)

    failures: list[str] = []
    if direction == "LONG" and (bear_4h or bear_1h):
        failures.append("HTF_OPPOSITION")
    elif direction == "SHORT" and (bull_4h or bull_1h):
        failures.append("HTF_OPPOSITION")

    strict = ((bull_4h and bull_1h) if direction == "LONG"
              else (bear_4h and bear_1h) if direction == "SHORT" else False)
    if strict:
        failures.append("CANONICAL_ALIGNMENT")
    if score_4h < min_4h:
        failures.append("SCORE_4H")
    if score_1h < min_1h:
        failures.append("SCORE_1H")
    if score_15m < min_15m:
        failures.append("SCORE_15M")
    if combined_score < min_combined:
        failures.append("SCORE_COMBINED")
    if not bool(s15.get("aligned")):
        failures.append("ALIGN_15M")
    if entry_type not in ("BOS_BREAK", "MOMENTUM"):
        failures.append("ENTRY_TYPE")
    if vol < min_vol:
        failures.append("VOLUME")
    if entry_type != "BOS_BREAK" and adx < min_adx:
        failures.append("ADX")
    if direction == "LONG" and rsi > 90:
        failures.append("RSI_LONG")
    if direction == "SHORT" and rsi < 10:
        failures.append("RSI_SHORT")
    if extension > max_extension:
        failures.append("EXTENSION")

    attribution = evidence_attribution(failures)
    return {
        "failures": failures,
        "failure_count": len(failures),
        "attribution": attribution,
        "score_4h": score_4h,
        "score_1h": score_1h,
        "score_15m": score_15m,
        "combined": combined_score,
        "volume_ratio": vol,
        "adx_15m": adx,
        "rsi_15m": rsi,
        "extension_atr": extension,
        "gap_4h": round(max(0.0, min_4h - score_4h), 4),
        "gap_1h": round(max(0.0, min_1h - score_1h), 4),
        "gap_15m": round(max(0.0, min_15m - score_15m), 4),
        "gap_combined": round(max(0.0, min_combined - combined_score), 4),
        "gap_volume": round(max(0.0, min_vol - vol), 4),
        "gap_adx": round(max(0.0, min_adx - adx), 4),
        "extension_excess": round(max(0.0, extension - max_extension), 4),
        "components_4h": _component_deficits(s4h),
        "components_1h": _component_deficits(s1h),
        "components_15m": _component_deficits(s15),
    }


def _fmt_components(deficits: dict[str, float]) -> str:
    return ",".join(f"{k}:{v:.1f}" for k, v in deficits.items())


def observe_reject(*, symbol: str, k15, direction: str, reason: str,
                   bull_4h: bool, bear_4h: bool, bull_1h: bool, bear_1h: bool,
                   s4h: dict, s1h: dict, s15: dict, combined: float,
                   entry_type: str, extension_atr: float,
                   thresholds: dict, log) -> None:
    if reason in {"canonical_alignment_already_present", "higher_timeframe_opposition",
                  "invalid_direction", "regime_direction_mismatch", "invalid_score"}:
        return
    if not _remember(symbol, k15):
        return

    snapshot = failure_vector(
        direction=direction,
        bull_4h=bull_4h, bear_4h=bear_4h,
        bull_1h=bull_1h, bear_1h=bear_1h,
        s4h=s4h, s1h=s1h, s15=s15,
        combined=combined, entry_type=entry_type,
        extension_atr=extension_atr, thresholds=thresholds,
    )
    failures = ",".join(snapshot["failures"]) or "NONE"
    attribution = snapshot["attribution"]
    families = ",".join(attribution["families"]) or "NONE"
    overlaps = ",".join(attribution["score_tf_overlap_checks"]) or "NONE"
    nexus_rechecks = ",".join(attribution["nexus_recheck_families"]) or "NONE"

    log.info(
        "[ADAPTIVE_MTF_CALIBRATION] symbol=%s side=%s reason=%s failures=%s "
        "failure_count=%d scores=4h:%.1f,1h:%.1f,15m:%.1f,combined:%.1f "
        "gaps=4h:%.1f,1h:%.1f,15m:%.1f,combined:%.1f "
        "vol=%.3fx vol_gap=%.3f adx=%.1f adx_gap=%.1f entry=%s "
        "extension=%.2fATR extension_excess=%.2f "
        "thresholds_unchanged=true leverage_unchanged=true "
        "decision_effect=NONE execution_effect=NONE",
        symbol, direction, reason, failures, snapshot["failure_count"],
        snapshot["score_4h"], snapshot["score_1h"], snapshot["score_15m"],
        snapshot["combined"], snapshot["gap_4h"], snapshot["gap_1h"],
        snapshot["gap_15m"], snapshot["gap_combined"],
        snapshot["volume_ratio"], snapshot["gap_volume"],
        snapshot["adx_15m"], snapshot["gap_adx"], entry_type,
        snapshot["extension_atr"], snapshot["extension_excess"],
    )
    log.info(
        "[ADAPTIVE_MTF_DUPLICATION] symbol=%s raw_failures=%d "
        "independent_families=%d collapsed_duplicates=%d families=%s "
        "score_tf_overlap_checks=%s score_tf_overlap_count=%d "
        "nexus_recheck_families=%s nexus_recheck_count=%d "
        "component_deficit_4h=%s component_deficit_1h=%s component_deficit_15m=%s "
        "thresholds_unchanged=true leverage_unchanged=true "
        "decision_effect=NONE execution_effect=NONE",
        symbol, attribution["raw_count"], attribution["independent_count"],
        attribution["collapsed_duplicates"], families, overlaps,
        attribution["score_tf_overlap_count"], nexus_rechecks,
        attribution["nexus_recheck_count"],
        _fmt_components(snapshot["components_4h"]),
        _fmt_components(snapshot["components_1h"]),
        _fmt_components(snapshot["components_15m"]),
    )
    _aggregate_snapshot(
        candle_ts=_closed_15m_ts(k15), snapshot=snapshot, log=log
    )
