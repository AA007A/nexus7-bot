"""Read-only calibration for Adaptive MTF HOLD decisions.

This module does not create Signals, call NEXUS, touch the exchange, or mutate
strategy thresholds. It records one compact reject snapshot per symbol and
confirmed 15m candle so we can distinguish a single near-threshold miss from a
setup that fails several adaptive requirements simultaneously.

It also keeps a process-local aggregate for each confirmed 15m candle. The
aggregate is observability only: it summarizes which gates fail most often and
how many candidates are genuine near-misses versus broad multi-gate rejects.
"""
from __future__ import annotations

import threading
from collections import Counter, deque
from typing import Any

_LOCK = threading.Lock()
_SEEN: set[tuple[str, object]] = set()
_SEEN_ORDER = deque(maxlen=5000)

# One process-local aggregate bucket per latest confirmed 15m candle. This is
# deliberately not persisted and never feeds a decision. A summary is emitted
# once the bucket has enough independent symbols to be informative, and the
# previous bucket is also flushed when a newer candle begins.
_AGG_TS: object | None = None
_AGG_FAILURES: Counter[str] = Counter()
_AGG_SAMPLES = 0
_AGG_NEAR_MISS_1 = 0
_AGG_NEAR_MISS_2 = 0
_AGG_EMITTED = False
_AGG_MIN_SAMPLES = 6


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
    # Same semantics used by Adaptive MTF: when the normal history is present,
    # the last element is forming and the previous element is confirmed.
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


def _format_top_failures(counter: Counter[str], limit: int = 6) -> str:
    if not counter:
        return "NONE"
    return ",".join(f"{name}:{count}" for name, count in counter.most_common(limit))


def _emit_aggregate(log, *, candle_ts: object, samples: int,
                    failures: Counter[str], near1: int, near2: int,
                    trigger: str) -> None:
    if samples <= 0:
        return
    multi = max(0, samples - near2)
    log.info(
        "[ADAPTIVE_MTF_FUNNEL_SUMMARY] candle=%s samples=%d "
        "near_miss_1=%d near_miss_2=%d multi_gate_gt2=%d "
        "top_failures=%s trigger=%s thresholds_unchanged=true "
        "leverage_unchanged=true decision_effect=NONE execution_effect=NONE",
        candle_ts, samples, near1, near2, multi,
        _format_top_failures(failures), trigger,
    )


def _aggregate_snapshot(*, candle_ts: object, snapshot: dict[str, Any], log) -> None:
    """Update/emit read-only per-candle rejection statistics."""
    global _AGG_TS, _AGG_FAILURES, _AGG_SAMPLES
    global _AGG_NEAR_MISS_1, _AGG_NEAR_MISS_2, _AGG_EMITTED

    with _LOCK:
        if _AGG_TS is not None and candle_ts != _AGG_TS:
            previous = (
                _AGG_TS, _AGG_SAMPLES, Counter(_AGG_FAILURES),
                _AGG_NEAR_MISS_1, _AGG_NEAR_MISS_2, _AGG_EMITTED,
            )
            _AGG_TS = candle_ts
            _AGG_FAILURES = Counter()
            _AGG_SAMPLES = 0
            _AGG_NEAR_MISS_1 = 0
            _AGG_NEAR_MISS_2 = 0
            _AGG_EMITTED = False
        else:
            previous = None
            if _AGG_TS is None:
                _AGG_TS = candle_ts

        failures = list(snapshot.get("failures") or [])
        count = int(snapshot.get("failure_count") or len(failures))
        _AGG_FAILURES.update(failures)
        _AGG_SAMPLES += 1
        if count <= 1:
            _AGG_NEAR_MISS_1 += 1
        if count <= 2:
            _AGG_NEAR_MISS_2 += 1

        immediate = None
        if _AGG_SAMPLES >= _AGG_MIN_SAMPLES and not _AGG_EMITTED:
            _AGG_EMITTED = True
            immediate = (
                _AGG_TS, _AGG_SAMPLES, Counter(_AGG_FAILURES),
                _AGG_NEAR_MISS_1, _AGG_NEAR_MISS_2,
            )

    # Logging happens outside the lock; no trading state is involved.
    if previous is not None:
        pts, psamples, pfailures, pnear1, pnear2, pemitted = previous
        if not pemitted:
            _emit_aggregate(
                log, candle_ts=pts, samples=psamples, failures=pfailures,
                near1=pnear1, near2=pnear2, trigger="candle_rollover",
            )
    if immediate is not None:
        its, isamples, ifailures, inear1, inear2 = immediate
        _emit_aggregate(
            log, candle_ts=its, samples=isamples, failures=ifailures,
            near1=inear1, near2=inear2, trigger="sample_threshold",
        )


def failure_vector(*, direction: str, bull_4h: bool, bear_4h: bool,
                   bull_1h: bool, bear_1h: bool, s4h: dict, s1h: dict,
                   s15: dict, combined: float, entry_type: str,
                   extension_atr: float, thresholds: dict) -> dict[str, Any]:
    """Return all adaptive-gate failures and numeric distances, not just first."""
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

    return {
        "failures": failures,
        "failure_count": len(failures),
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
    }


def observe_reject(*, symbol: str, k15, direction: str, reason: str,
                   bull_4h: bool, bear_4h: bool, bull_1h: bool, bear_1h: bool,
                   s4h: dict, s1h: dict, s15: dict, combined: float,
                   entry_type: str, extension_atr: float,
                   thresholds: dict, log) -> None:
    """Emit one deduplicated structured reject snapshot per confirmed 15m bar."""
    # Canonical-alignment cases are HOLD for a different canonical reason, and
    # explicit HTF opposition is intentionally non-negotiable. Neither is a
    # threshold-calibration cohort.
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
    _aggregate_snapshot(
        candle_ts=_closed_15m_ts(k15), snapshot=snapshot, log=log
    )
