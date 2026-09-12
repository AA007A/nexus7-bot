"""Read-only counterfactual calibration for the pre-NEXUS session score penalty.

Production remains authoritative. This module observes only Analyzer signals that
would be discarded by TradingEngine's existing session penalty and follows their
subsequent confirmed 15m path. It never creates a Signal, calls NEXUS, changes a
threshold, or mutates exchange/risk state.
"""
from __future__ import annotations

import threading
from collections import Counter

from bot.kucoin_execution_model import estimated_round_trip_cost_pct
from bot.nexus_decision_consistency import closed_mtf

_MAX_BARS = 16
_LOCK = threading.Lock()
_SEEN: set[str] = set()
_ACTIVE: dict[str, dict] = {}
_METRICS = {
    "unique": 0,
    "eligible": 0,
    "resolved": 0,
    "outcomes": Counter(),
    "sessions": Counter(),
    "symbols": Counter(),
}


def _finite(value, default=0.0):
    try:
        out = float(value)
        return out if out == out and abs(out) != float("inf") else default
    except (TypeError, ValueError):
        return default


def _closed_15m(k15, k1h=None, k4h=None):
    closed, _, _ = closed_mtf(k15, k1h or [], k4h or [])
    return closed


def _state_key(symbol: str, direction: str, bar_ts) -> str:
    return f"{symbol}:{str(direction).upper()}:{bar_ts}"


def _directional_excursions(state: dict, high: float, low: float) -> tuple[float, float]:
    entry = _finite(state.get("entry"))
    if entry <= 0:
        return 0.0, 0.0
    if state.get("direction") == "LONG":
        favorable = (high - entry) / entry * 100.0
        adverse = (low - entry) / entry * 100.0
    else:
        favorable = (entry - low) / entry * 100.0
        adverse = (entry - high) / entry * 100.0
    return favorable, adverse


def _resolve(state: dict, outcome: str, exit_price: float, log) -> None:
    entry = _finite(state.get("entry"))
    if entry <= 0:
        net_pct = 0.0
    else:
        raw = (exit_price - entry) / entry * 100.0
        if state.get("direction") == "SHORT":
            raw = -raw
        net_pct = raw - estimated_round_trip_cost_pct(state.get("symbol", "BTCUSDT"))

    state["outcome"] = outcome
    state["net_pct"] = round(net_pct, 5)
    _METRICS["resolved"] += 1
    _METRICS["outcomes"][outcome] += 1
    log.info(
        "[SESSION_PENALTY_SHADOW_OUTCOME] symbol=%s side=%s session=%s "
        "penalty=%+d score=%s->%s outcome=%s net_pct=%.4f mfe=%.4f mae=%.4f "
        "bars=%d decision_effect=NONE execution_effect=NONE",
        state.get("symbol"), state.get("direction"), state.get("session"),
        int(state.get("penalty", 0)), state.get("base_score"),
        state.get("adjusted_score"), outcome, state.get("net_pct", 0.0),
        state.get("mfe_pct", 0.0), state.get("mae_pct", 0.0),
        int(state.get("bars", 0)),
    )


def _update_outcomes(symbol: str, k15, k1h, k4h, log) -> None:
    closed = _closed_15m(k15, k1h, k4h)
    if not closed:
        return
    with _LOCK:
        for key, state in list(_ACTIVE.items()):
            if state.get("symbol") != symbol:
                continue
            last_ts = state.get("last_bar_ts")
            for bar in closed:
                ts = bar.get("ts")
                if ts is None or (last_ts is not None and ts <= last_ts):
                    continue
                high = _finite(bar.get("h"))
                low = _finite(bar.get("l"))
                if high <= 0 or low <= 0:
                    continue
                state["last_bar_ts"] = ts
                state["bars"] += 1
                favorable, adverse = _directional_excursions(state, high, low)
                state["mfe_pct"] = max(_finite(state.get("mfe_pct")), favorable)
                state["mae_pct"] = min(_finite(state.get("mae_pct")), adverse)

                if state.get("direction") == "LONG":
                    tp_hit = high >= state["tp"]
                    sl_hit = low <= state["sl"]
                else:
                    tp_hit = low <= state["tp"]
                    sl_hit = high >= state["sl"]

                # Conservative same-bar ordering: stop first.
                if tp_hit and sl_hit:
                    _resolve(state, "AMBIGUOUS_STOP_FIRST", state["sl"], log)
                    _ACTIVE.pop(key, None)
                    break
                if sl_hit:
                    _resolve(state, "SL", state["sl"], log)
                    _ACTIVE.pop(key, None)
                    break
                if tp_hit:
                    _resolve(state, "TP", state["tp"], log)
                    _ACTIVE.pop(key, None)
                    break
                if state["bars"] >= _MAX_BARS:
                    last = _finite(bar.get("c"), state["entry"])
                    _resolve(state, "TIMEOUT", last, log)
                    _ACTIVE.pop(key, None)
                    break


def observe(symbol: str, k15, k1h, k4h, *, production_result,
            min_score: int, log) -> None:
    """Observe one canonical Analyzer evaluation and return no trading value."""
    _update_outcomes(symbol, k15, k1h, k4h, log)
    if production_result is None:
        return

    try:
        from bot.engine import TradingEngine
        session = TradingEngine._get_market_session()
        penalty = int(TradingEngine._SESSION_PENALTY.get(session, {}).get(symbol, 0))
    except Exception as exc:
        log.debug(
            "[SESSION_PENALTY_SHADOW] policy_read_failed error=%s "
            "decision_effect=NONE execution_effect=NONE",
            type(exc).__name__,
        )
        return

    base_score = int(getattr(production_result, "score", 0) or 0)
    adjusted = max(0, base_score + penalty)
    if penalty >= 0 or base_score < int(min_score) or adjusted >= int(min_score):
        return

    direction = str(getattr(production_result, "direction", "")).upper()
    entry = _finite(getattr(production_result, "entry", 0.0))
    sl = _finite(getattr(production_result, "sl", 0.0))
    tp = _finite(getattr(production_result, "tp", 0.0))
    if direction not in {"LONG", "SHORT"} or min(entry, sl, tp) <= 0:
        return

    closed = _closed_15m(k15, k1h, k4h)
    if not closed or closed[-1].get("ts") is None:
        return
    bar_ts = closed[-1]["ts"]
    key = _state_key(symbol, direction, bar_ts)

    with _LOCK:
        if key in _SEEN:
            return
        _SEEN.add(key)
        state = {
            "symbol": symbol,
            "direction": direction,
            "session": session,
            "penalty": penalty,
            "base_score": base_score,
            "adjusted_score": adjusted,
            "min_score": int(min_score),
            "entry": entry,
            "sl": sl,
            "tp": tp,
            "opened_bar_ts": bar_ts,
            "last_bar_ts": bar_ts,
            "bars": 0,
            "mfe_pct": 0.0,
            "mae_pct": 0.0,
            "entry_type": str(getattr(production_result, "entry_type", "UNKNOWN")),
            "regime": str(getattr(production_result, "regime", "UNKNOWN")),
        }
        _ACTIVE[key] = state
        _METRICS["unique"] += 1
        _METRICS["eligible"] += 1
        _METRICS["sessions"][session] += 1
        _METRICS["symbols"][symbol] += 1

    log.info(
        "[SESSION_PENALTY_SHADOW] symbol=%s side=%s session=%s penalty=%+d "
        "score=%d->%d min=%d entry_type=%s regime=%s tracking_bars=%d "
        "production_policy_unchanged=true decision_effect=NONE execution_effect=NONE",
        symbol, direction, session, penalty, base_score, adjusted, int(min_score),
        state["entry_type"], state["regime"], _MAX_BARS,
    )


def snapshot() -> dict:
    with _LOCK:
        return {
            "unique": int(_METRICS["unique"]),
            "eligible": int(_METRICS["eligible"]),
            "resolved": int(_METRICS["resolved"]),
            "active": len(_ACTIVE),
            "outcomes": dict(_METRICS["outcomes"]),
            "sessions": dict(_METRICS["sessions"]),
            "symbols": dict(_METRICS["symbols"]),
        }
