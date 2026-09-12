"""Read-only counterfactual score_tf rebalancing.

The live strategy remains untouched. This module recomputes only the top-level
component weights using the exact component scores already produced by
strategy.score_tf and the same Adaptive thresholds/timeframe weights.

Profile rationale: trend and volume are heavily represented upstream and are
re-evaluated again by Adaptive/NEXUS. The shadow reduces their aggregate weight
from 50% to 40% and redistributes 10 points to volatility/structure. It never
creates a Signal, calls NEXUS, changes thresholds, or affects execution.
"""
from __future__ import annotations

import threading
from collections import Counter, deque
from typing import Any

_CURRENT_MAX = {
    "trend_s": 30.0,
    "vol_s": 20.0,
    "momentum_s": 20.0,
    "atr_s": 15.0,
    "struct_s": 15.0,
}
_REBALANCED_WEIGHTS = {
    "trend_s": 25.0,
    "vol_s": 15.0,
    "momentum_s": 20.0,
    "atr_s": 20.0,
    "struct_s": 20.0,
}
_SCORE_FAILURES = {"SCORE_4H", "SCORE_1H", "SCORE_15M", "SCORE_COMBINED"}

_LOCK = threading.Lock()
_SEEN: set[tuple[str, object]] = set()
_SEEN_ORDER = deque(maxlen=5000)
_AGG_TS: object | None = None
_AGG_SAMPLES = 0
_AGG_SCORE_FAMILY_CLEARED = 0
_AGG_ALL_FAILURES_CLEARED = 0
_AGG_REMAINING: Counter[str] = Counter()
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


def rebalanced_score(score: dict) -> float:
    total = 0.0
    for key, maximum in _CURRENT_MAX.items():
        raw = max(0.0, min(maximum, _finite(score.get(key))))
        quality = raw / maximum if maximum > 0 else 0.0
        total += quality * _REBALANCED_WEIGHTS[key]
    return round(total, 2)


def assess(*, s4h: dict, s1h: dict, s15: dict, failures: list[str], thresholds: dict) -> dict[str, Any]:
    r4 = rebalanced_score(s4h)
    r1 = rebalanced_score(s1h)
    r15 = rebalanced_score(s15)
    combined = round(r4 * 0.25 + r1 * 0.30 + r15 * 0.45, 2)

    score_failures: list[str] = []
    if r4 < _finite(thresholds.get("min_4h")):
        score_failures.append("SCORE_4H")
    if r1 < _finite(thresholds.get("min_1h")):
        score_failures.append("SCORE_1H")
    if r15 < _finite(thresholds.get("min_15m")):
        score_failures.append("SCORE_15M")
    if combined < _finite(thresholds.get("min_combined")):
        score_failures.append("SCORE_COMBINED")

    non_score = [f for f in failures if f not in _SCORE_FAILURES]
    remaining = non_score + score_failures
    return {
        "current": {
            "4h": _finite(s4h.get("total")),
            "1h": _finite(s1h.get("total")),
            "15m": _finite(s15.get("total")),
        },
        "rebalanced": {"4h": r4, "1h": r1, "15m": r15, "combined": combined},
        "score_failures": score_failures,
        "remaining_failures": remaining,
        "score_family_cleared": not score_failures,
        "all_failures_cleared": not remaining,
    }


def _fmt(items: list[str]) -> str:
    return ",".join(items) if items else "NONE"


def _fmt_counter(counter: Counter[str], limit: int = 6) -> str:
    if not counter:
        return "NONE"
    return ",".join(f"{k}:{v}" for k, v in counter.most_common(limit))


def _emit_summary(log, *, candle_ts: object, samples: int, score_cleared: int,
                  all_cleared: int, remaining: Counter[str], trigger: str) -> None:
    if samples <= 0:
        return
    log.info(
        "[SCORE_TF_REBALANCED_SHADOW_SUMMARY] candle=%s samples=%d "
        "score_family_cleared=%d score_clear_rate=%.1f%% all_failures_cleared=%d "
        "top_remaining=%s trigger=%s profile=trend25_volume15_momentum20_volatility20_structure20 "
        "shadow_only=true nexus_called=false signal_created=false thresholds_unchanged=true "
        "leverage_unchanged=true decision_effect=NONE execution_effect=NONE",
        candle_ts, samples, score_cleared,
        100.0 * score_cleared / samples if samples else 0.0,
        all_cleared, _fmt_counter(remaining), trigger,
    )


def _aggregate(*, candle_ts: object, result: dict[str, Any], log) -> None:
    global _AGG_TS, _AGG_SAMPLES, _AGG_SCORE_FAMILY_CLEARED
    global _AGG_ALL_FAILURES_CLEARED, _AGG_REMAINING, _AGG_EMITTED
    with _LOCK:
        if _AGG_TS is not None and candle_ts != _AGG_TS:
            previous = (_AGG_TS, _AGG_SAMPLES, _AGG_SCORE_FAMILY_CLEARED,
                        _AGG_ALL_FAILURES_CLEARED, Counter(_AGG_REMAINING), _AGG_EMITTED)
            _AGG_TS = candle_ts
            _AGG_SAMPLES = 0
            _AGG_SCORE_FAMILY_CLEARED = 0
            _AGG_ALL_FAILURES_CLEARED = 0
            _AGG_REMAINING = Counter()
            _AGG_EMITTED = False
        else:
            previous = None
            if _AGG_TS is None:
                _AGG_TS = candle_ts

        _AGG_SAMPLES += 1
        if result["score_family_cleared"]:
            _AGG_SCORE_FAMILY_CLEARED += 1
        if result["all_failures_cleared"]:
            _AGG_ALL_FAILURES_CLEARED += 1
        _AGG_REMAINING.update(result["remaining_failures"])

        immediate = None
        if _AGG_SAMPLES >= _AGG_MIN_SAMPLES and not _AGG_EMITTED:
            _AGG_EMITTED = True
            immediate = (_AGG_TS, _AGG_SAMPLES, _AGG_SCORE_FAMILY_CLEARED,
                         _AGG_ALL_FAILURES_CLEARED, Counter(_AGG_REMAINING))

    if previous is not None:
        pts, ps, psc, pac, prem, pemitted = previous
        if not pemitted:
            _emit_summary(log, candle_ts=pts, samples=ps, score_cleared=psc,
                          all_cleared=pac, remaining=prem, trigger="candle_rollover")
    if immediate is not None:
        its, ins, isc, iac, irem = immediate
        _emit_summary(log, candle_ts=its, samples=ins, score_cleared=isc,
                      all_cleared=iac, remaining=irem, trigger="sample_threshold")


def observe_reject(*, symbol: str, k15, s4h: dict, s1h: dict, s15: dict,
                   failures: list[str], thresholds: dict, log) -> None:
    if not _remember(symbol, k15):
        return
    result = assess(s4h=s4h, s1h=s1h, s15=s15, failures=failures, thresholds=thresholds)
    cur = result["current"]
    reb = result["rebalanced"]
    log.info(
        "[SCORE_TF_REBALANCED_SHADOW] symbol=%s current=4h:%.1f,1h:%.1f,15m:%.1f "
        "rebalanced=4h:%.2f,1h:%.2f,15m:%.2f,combined:%.2f "
        "delta=4h:%+.2f,1h:%+.2f,15m:%+.2f score_blockers=%s remaining=%s "
        "score_family_cleared=%s all_failures_cleared=%s "
        "profile=trend25_volume15_momentum20_volatility20_structure20 "
        "shadow_only=true nexus_called=false signal_created=false thresholds_unchanged=true "
        "leverage_unchanged=true decision_effect=NONE execution_effect=NONE",
        symbol, cur["4h"], cur["1h"], cur["15m"], reb["4h"], reb["1h"], reb["15m"],
        reb["combined"], reb["4h"]-cur["4h"], reb["1h"]-cur["1h"], reb["15m"]-cur["15m"],
        _fmt(result["score_failures"]), _fmt(result["remaining_failures"]),
        str(result["score_family_cleared"]).lower(), str(result["all_failures_cleared"]).lower(),
    )
    _aggregate(candle_ts=_closed_15m_ts(k15), result=result, log=log)
