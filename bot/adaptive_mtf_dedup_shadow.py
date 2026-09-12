"""Read-only Adaptive MTF deduplicated shadow evaluator.

This module answers one narrow question: if Adaptive kept its score thresholds
and independent safety/structure checks but did not hard-reject evidence already
embedded in ``strategy.score_tf``, would the candidate have reached the NEXUS
review stage?

It never creates a Signal, never calls NEXUS, never touches risk/execution, and
never mutates thresholds.  It is observability only.
"""
from __future__ import annotations

import threading
from collections import Counter, deque
from typing import Any

from bot import adaptive_mtf_calibration

# These hard checks reuse evidence already represented inside score_tf.
# Removing them in shadow does NOT remove the score thresholds themselves.
_DEDUP_REMOVABLE = {
    "ALIGN_15M",
    "VOLUME",
    "ADX",
    "RSI_LONG",
    "RSI_SHORT",
}

_LOCK = threading.Lock()
_SEEN: set[tuple[str, object]] = set()
_SEEN_ORDER = deque(maxlen=5000)
_AGG_TS: object | None = None
_AGG_SAMPLES = 0
_AGG_WOULD_REACH_NEXUS = 0
_AGG_REMOVED: Counter[str] = Counter()
_AGG_REMAINING: Counter[str] = Counter()
_AGG_EMITTED = False
_AGG_MIN_SAMPLES = 6


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


def _fmt(items: list[str]) -> str:
    return ",".join(items) if items else "NONE"


def _fmt_counter(counter: Counter[str], limit: int = 6) -> str:
    if not counter:
        return "NONE"
    return ",".join(f"{name}:{count}" for name, count in counter.most_common(limit))


def assess_failures(failures: list[str]) -> dict[str, Any]:
    """Return the strict-vs-deduplicated shadow decision.

    Only checks in ``_DEDUP_REMOVABLE`` are ignored. Score thresholds and all
    independent structural/safety checks remain authoritative in the shadow.
    """
    raw = list(failures or [])
    removed = [name for name in raw if name in _DEDUP_REMOVABLE]
    remaining = [name for name in raw if name not in _DEDUP_REMOVABLE]
    return {
        "strict_failures": raw,
        "removed_duplicate_checks": removed,
        "remaining_failures": remaining,
        "strict_failure_count": len(raw),
        "dedup_failure_count": len(remaining),
        "would_reach_nexus": len(remaining) == 0,
    }


def _emit_summary(log, *, candle_ts: object, samples: int, would_reach: int,
                  removed: Counter[str], remaining: Counter[str],
                  trigger: str) -> None:
    if samples <= 0:
        return
    log.info(
        "[ADAPTIVE_DEDUP_SHADOW_SUMMARY] candle=%s samples=%d "
        "would_reach_nexus=%d still_hold=%d reach_rate=%.1f%% "
        "top_removed_duplicates=%s top_remaining_blockers=%s trigger=%s "
        "shadow_only=true nexus_called=false signal_created=false "
        "thresholds_unchanged=true leverage_unchanged=true "
        "decision_effect=NONE execution_effect=NONE",
        candle_ts, samples, would_reach, max(0, samples - would_reach),
        (100.0 * would_reach / samples) if samples else 0.0,
        _fmt_counter(removed), _fmt_counter(remaining), trigger,
    )


def _aggregate(*, candle_ts: object, result: dict[str, Any], log) -> None:
    global _AGG_TS, _AGG_SAMPLES, _AGG_WOULD_REACH_NEXUS
    global _AGG_REMOVED, _AGG_REMAINING, _AGG_EMITTED

    with _LOCK:
        if _AGG_TS is not None and candle_ts != _AGG_TS:
            previous = (
                _AGG_TS, _AGG_SAMPLES, _AGG_WOULD_REACH_NEXUS,
                Counter(_AGG_REMOVED), Counter(_AGG_REMAINING), _AGG_EMITTED,
            )
            _AGG_TS = candle_ts
            _AGG_SAMPLES = 0
            _AGG_WOULD_REACH_NEXUS = 0
            _AGG_REMOVED = Counter()
            _AGG_REMAINING = Counter()
            _AGG_EMITTED = False
        else:
            previous = None
            if _AGG_TS is None:
                _AGG_TS = candle_ts

        _AGG_SAMPLES += 1
        if bool(result.get("would_reach_nexus")):
            _AGG_WOULD_REACH_NEXUS += 1
        _AGG_REMOVED.update(result.get("removed_duplicate_checks") or [])
        _AGG_REMAINING.update(result.get("remaining_failures") or [])

        immediate = None
        if _AGG_SAMPLES >= _AGG_MIN_SAMPLES and not _AGG_EMITTED:
            _AGG_EMITTED = True
            immediate = (
                _AGG_TS, _AGG_SAMPLES, _AGG_WOULD_REACH_NEXUS,
                Counter(_AGG_REMOVED), Counter(_AGG_REMAINING),
            )

    if previous is not None:
        pts, psamples, preach, premoved, premaining, pemitted = previous
        if not pemitted:
            _emit_summary(
                log, candle_ts=pts, samples=psamples, would_reach=preach,
                removed=premoved, remaining=premaining,
                trigger="candle_rollover",
            )
    if immediate is not None:
        its, isamples, ireach, iremoved, iremaining = immediate
        _emit_summary(
            log, candle_ts=its, samples=isamples, would_reach=ireach,
            removed=iremoved, remaining=iremaining,
            trigger="sample_threshold",
        )


def observe_reject(*, symbol: str, k15, direction: str,
                   bull_4h: bool, bear_4h: bool,
                   bull_1h: bool, bear_1h: bool,
                   s4h: dict, s1h: dict, s15: dict, combined: float,
                   entry_type: str, extension_atr: float,
                   thresholds: dict, log) -> None:
    """Log what would happen under duplicate-only relaxation.

    This function intentionally mirrors the same failure-vector inputs as the
    strict Adaptive calibration, then removes only score_tf-overlap hard checks.
    """
    if not _remember(symbol, k15):
        return

    snapshot = adaptive_mtf_calibration.failure_vector(
        direction=direction,
        bull_4h=bull_4h, bear_4h=bear_4h,
        bull_1h=bull_1h, bear_1h=bear_1h,
        s4h=s4h, s1h=s1h, s15=s15,
        combined=combined, entry_type=entry_type,
        extension_atr=extension_atr, thresholds=thresholds,
    )
    result = assess_failures(list(snapshot.get("failures") or []))

    log.info(
        "[ADAPTIVE_DEDUP_SHADOW] symbol=%s side=%s strict_failures=%d "
        "removed_duplicates=%s dedup_failures=%d remaining_blockers=%s "
        "would_reach_nexus=%s shadow_only=true nexus_called=false "
        "signal_created=false thresholds_unchanged=true leverage_unchanged=true "
        "decision_effect=NONE execution_effect=NONE",
        symbol, direction, result["strict_failure_count"],
        _fmt(result["removed_duplicate_checks"]), result["dedup_failure_count"],
        _fmt(result["remaining_failures"]),
        str(bool(result["would_reach_nexus"])).lower(),
    )
    _aggregate(candle_ts=_closed_15m_ts(k15), result=result, log=log)
