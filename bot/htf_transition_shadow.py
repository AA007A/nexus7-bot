"""Read-only shadow audit for fast higher-timeframe transitions.

Production remains unchanged. This observer studies the exact class of missed
opportunity seen during strong market accelerations: the last *confirmed* 4H
bar still points opposite/neutral while the currently-forming 4H state has
flipped into agreement with confirmed 1H and 15M momentum.

Nothing in this module can return a Signal or call an exchange method. It only
logs one deduplicated shadow sample per confirmed 15m bar and follows subsequent
confirmed 15m OHLC to measure MFE/MAE and hypothetical TP/SL outcomes.
"""
from __future__ import annotations

import hashlib
import json
import math
import threading
import time
from collections import Counter, deque

import numpy as np

from bot.indicators import atr, ema
from bot.strategy import TOTAL_COST, detect_entry, score_tf

_LOCK = threading.Lock()
_SEEN: set[tuple] = set()
_SEEN_ORDER = deque(maxlen=5000)
_ACTIVE: dict[str, dict] = {}
_MAX_ACTIVE = 1000
_MAX_BARS = 16  # 4h of 15m confirmed bars
_METRICS = {
    "unique": 0,
    "eligible": 0,
    "resolved": 0,
    "outcomes": Counter(),
    "net_sum": 0.0,
    "mfe_sum": 0.0,
    "mae_sum": 0.0,
}


def _closed(kl):
    data = list(kl or [])
    return data[:-1] if len(data) > 2 else data


def _ts(bar):
    if not bar:
        return None
    return bar.get("ts") or bar.get("time") or bar.get("timestamp")


def _ga(kl):
    return (
        [float(k["c"]) for k in kl],
        [float(k["h"]) for k in kl],
        [float(k["l"]) for k in kl],
        [float(k["o"]) for k in kl],
        [float(k.get("v", 0) or 0) for k in kl],
    )


def _atr_pair(h, l, c):
    values = atr(h, l, c)
    now = float(values[-1])
    avg = float(np.mean(values[-20:])) if len(values) >= 20 else now
    return now, avg


def _ema_state(c):
    if len(c) < 51:
        return "NEUTRAL"
    e20 = float(ema(c, 20)[-1]); e50 = float(ema(c, 50)[-1]); px = float(c[-1])
    if not all(math.isfinite(v) for v in (e20, e50, px)):
        return "NEUTRAL"
    if e20 > e50 and px > e20:
        return "LONG"
    if e20 < e50 and px < e20:
        return "SHORT"
    return "NEUTRAL"


def _key(symbol, direction, closed15_ts):
    return (symbol, direction, closed15_ts)


def _remember(key):
    with _LOCK:
        if key in _SEEN:
            return False
        if len(_SEEN_ORDER) == _SEEN_ORDER.maxlen:
            old = _SEEN_ORDER.popleft(); _SEEN.discard(old)
        _SEEN.add(key); _SEEN_ORDER.append(key); _METRICS["unique"] += 1
        return True


def _sid(key):
    return hashlib.sha256("|".join(map(str, key)).encode()).hexdigest()[:16]


def _emit(log, tag, payload):
    log.info("[%s] %s", tag, json.dumps(payload, sort_keys=True, separators=(",", ":")))


def _update_outcomes(symbol, k15, log):
    closed = _closed(k15)
    if not closed:
        return
    bar = closed[-1]
    bar_ts = _ts(bar)
    try:
        hi, lo, close = float(bar["h"]), float(bar["l"]), float(bar["c"])
    except Exception:
        return

    with _LOCK:
        ids = [sid for sid, st in _ACTIVE.items() if st["symbol"] == symbol]
    for sid in ids:
        with _LOCK:
            st = _ACTIVE.get(sid)
            if not st or st.get("last_bar_ts") == bar_ts:
                continue
            if st.get("opened_bar_ts") == bar_ts:
                st["last_bar_ts"] = bar_ts
                continue
            st["last_bar_ts"] = bar_ts
            st["bars"] += 1
            entry, sl, tp = st["entry"], st["sl"], st["tp"]
            if st["direction"] == "LONG":
                fav, adv = max(0.0, hi-entry), max(0.0, entry-lo)
                hit_tp, hit_sl = hi >= tp, lo <= sl
            else:
                fav, adv = max(0.0, entry-lo), max(0.0, hi-entry)
                hit_tp, hit_sl = lo <= tp, hi >= sl
            st["mfe_pct"] = max(st["mfe_pct"], fav/entry*100 if entry else 0.0)
            st["mae_pct"] = max(st["mae_pct"], adv/entry*100 if entry else 0.0)
            bars = st["bars"]

        outcome = None
        if hit_tp and hit_sl: outcome = "AMBIGUOUS_BOTH"
        elif hit_tp: outcome = "TP"
        elif hit_sl: outcome = "SL"
        elif bars >= _MAX_BARS: outcome = "TIMEOUT_4H"
        if not outcome:
            continue

        exit_px = tp if outcome == "TP" else sl if outcome == "SL" else close
        raw = ((exit_px-entry)/entry*100) * (1 if st["direction"] == "LONG" else -1)
        net = raw - TOTAL_COST*100
        payload = {
            "state_id": sid, "symbol": symbol, "direction": st["direction"],
            "outcome": outcome, "entry": round(entry, 10), "exit": round(exit_px, 10),
            "mfe_pct": round(st["mfe_pct"], 5), "mae_pct": round(st["mae_pct"], 5),
            "net_pct_after_cost_model": round(net, 5), "bars": bars,
            "execution_effect": "NONE",
        }
        with _LOCK:
            _ACTIVE.pop(sid, None)
            _METRICS["resolved"] += 1; _METRICS["outcomes"][outcome] += 1
            _METRICS["net_sum"] += net; _METRICS["mfe_sum"] += st["mfe_pct"]
            _METRICS["mae_sum"] += st["mae_pct"]
        _emit(log, "HTF_TRANSITION_OUTCOME", payload)


def observe(symbol, k15, k1h, k4h, production_result, log):
    """Observe one strategy evaluation without changing its result."""
    try:
        _update_outcomes(symbol, k15, log)
        if production_result is not None:
            return
        if len(k15) < 60 or len(k1h) < 60 or len(k4h) < 60:
            return

        c15,h15,l15,o15,v15 = _ga(_closed(k15))
        c1,h1,l1,o1,v1 = _ga(_closed(k1h))
        c4_closed,h4,l4,o4,v4 = _ga(_closed(k4h))
        c4_live,h4_live,l4_live,o4_live,v4_live = _ga(list(k4h))

        state_15 = _ema_state(c15); state_1h = _ema_state(c1)
        state_4h_closed = _ema_state(c4_closed); state_4h_live = _ema_state(c4_live)
        if state_15 not in ("LONG", "SHORT") or state_1h != state_15:
            return
        direction = state_15
        # Transition exists only if confirmed 4H has NOT yet aligned but the
        # forming 4H has flipped into the 1H/15M direction.
        if state_4h_closed == direction or state_4h_live != direction:
            return

        closed_ts = _ts(_closed(k15)[-1])
        key = _key(symbol, direction, closed_ts)
        if not _remember(key):
            return

        atr15, avg15 = _atr_pair(h15,l15,c15)
        atr1, avg1 = _atr_pair(h1,l1,c1)
        atr4, avg4 = _atr_pair(h4_live,l4_live,c4_live)
        s15 = score_tf(c15,h15,l15,o15,v15,direction,atr15,avg15)
        s1 = score_tf(c1,h1,l1,o1,v1,direction,atr1,avg1)
        s4_live = score_tf(c4_live,h4_live,l4_live,o4_live,v4_live,direction,atr4,avg4)
        if not all(x.get("ok") for x in (s15,s1,s4_live)):
            return
        combined = round(s4_live["total"]*.25 + s1["total"]*.30 + s15["total"]*.45)
        entry_ok, entry_type = detect_entry(c15,h15,l15,o15,v15,direction,atr15)
        # Deliberately strict shadow cohort: study only the strongest transition
        # cases first, not every temporary forming-4H flip.
        if (combined < 72 or s1["total"] < 70 or s15["total"] < 80 or
                float(s15.get("vol_r",0)) < 1.20 or
                (entry_type != "BOS_BREAK" and float(s15.get("adx_v",0)) < 18) or
                not entry_ok or entry_type not in ("BOS_BREAK","MOMENTUM")):
            return

        price = float(c15[-1]); sl_atr = max(atr15, atr1*.5)
        if entry_type == "BOS_BREAK": sl_mult,tp_mult = 1.2,3.6
        else: sl_mult,tp_mult = 1.5,3.0
        if direction == "LONG": sl, tp = price-sl_atr*sl_mult, price+sl_atr*tp_mult
        else: sl, tp = price+sl_atr*sl_mult, price-sl_atr*tp_mult
        state_id = _sid(key)
        payload = {
            "state_id": state_id, "symbol": symbol, "direction": direction,
            "closed_4h_state": state_4h_closed, "forming_4h_state": state_4h_live,
            "confirmed_1h_state": state_1h, "confirmed_15m_state": state_15,
            "score": combined, "score_4h_forming": s4_live["total"],
            "score_1h": s1["total"], "score_15m": s15["total"],
            "volume_ratio_15m": round(float(s15.get("vol_r",0)),4),
            "adx_15m": round(float(s15.get("adx_v",0)),2), "entry_type": entry_type,
            "entry": round(price,10), "sl": round(sl,10), "tp": round(tp,10),
            "policy": "SHADOW_ONLY_FORMING_4H_TRANSITION",
            "execution_effect": "NONE",
        }
        with _LOCK:
            _METRICS["eligible"] += 1
            if len(_ACTIVE) >= _MAX_ACTIVE:
                oldest = next(iter(_ACTIVE)); _ACTIVE.pop(oldest, None)
            _ACTIVE[state_id] = {
                **payload, "opened_bar_ts": closed_ts, "last_bar_ts": closed_ts,
                "bars": 0, "mfe_pct": 0.0, "mae_pct": 0.0,
            }
        _emit(log, "HTF_TRANSITION_SHADOW", payload)
    except Exception as exc:
        log.debug("[HTF_TRANSITION_SHADOW] observer_error=%s execution_effect=NONE", type(exc).__name__)


def snapshot():
    with _LOCK:
        resolved = int(_METRICS["resolved"])
        return {
            "unique": int(_METRICS["unique"]), "eligible": int(_METRICS["eligible"]),
            "active": len(_ACTIVE), "resolved": resolved,
            "outcomes": dict(_METRICS["outcomes"]),
            "avg_net_pct": round(_METRICS["net_sum"]/resolved,5) if resolved else None,
            "avg_mfe_pct": round(_METRICS["mfe_sum"]/resolved,5) if resolved else None,
            "avg_mae_pct": round(_METRICS["mae_sum"]/resolved,5) if resolved else None,
        }
