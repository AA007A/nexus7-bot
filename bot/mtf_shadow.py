"""Shadow A/B evaluator for the strict 4H/1H alignment gate.

Policy A (production): unchanged. Analyzer.analyze_mtf keeps requiring explicit
4H + 1H alignment.

Policy B (shadow only): permits 4H directional + 1H neutral, never 1H opposite.
The shadow candidate is then run through the same downstream strategy filters
(score, RSI, volume, entry trigger, R:R, costs) and through nexus_ai.decide().
No Signal is returned to the trading engine and no order path is invoked.

This module also tracks outcome telemetry for shadow-only survivors. It never
submits, amends, cancels, or routes orders and never alters production gates.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from collections import Counter, deque

import numpy as np

from bot.config import cfg
from bot.indicators import atr, ema
from bot.strategy import TOTAL_COST, detect_entry, detect_regime, score_tf
from bot import nexus_ai

log = logging.getLogger(__name__)
_LOCK = threading.Lock()
_SEEN = set()
_SEEN_ORDER = deque(maxlen=5000)
_ACTIVE = {}
_MAX_ACTIVE = 1000
_MAX_OUTCOME_BARS = 96

_METRICS = {
    "started_at": time.time(), "raw_calls": 0, "unique_states": 0,
    "policy_a_signals": 0, "policy_a_holds": 0,
    "eligible_4h_dir_1h_neutral": 0, "blocked_1h_opposite": 0,
    "shadow_pre_ai_survivors": 0, "shadow_nexus_approved": 0,
    "shadow_nexus_vetoed": 0, "shadow_blocks": Counter(),
    "symbols": Counter(), "nexus_reasons": Counter(), "outcomes": Counter(),
    "resolved_outcomes": 0, "resolved_r_sum": 0.0,
    "resolved_net_pct_sum": 0.0, "mfe_pct_sum": 0.0, "mae_pct_sum": 0.0,
    "last": [], "last_outcomes": [],
}


def _closed_bar(klines):
    if not klines:
        return None
    idx = -2 if len(klines) > 1 else -1
    return klines[idx]


def _closed_ts(klines):
    if not klines:
        return None
    idx = -2 if len(klines) > 1 else -1
    k = klines[idx]
    return k.get("ts") or k.get("time") or k.get("timestamp") or idx


def _unique_key(symbol, k15, k1h, k4h):
    return (symbol, _closed_ts(k15), _closed_ts(k1h), _closed_ts(k4h))


def _state_id(key):
    raw = "|".join(str(v) for v in key)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _remember_unique(key):
    with _LOCK:
        _METRICS["raw_calls"] += 1
        if key in _SEEN:
            return False
        if len(_SEEN_ORDER) == _SEEN_ORDER.maxlen:
            old = _SEEN_ORDER.popleft(); _SEEN.discard(old)
        _SEEN.add(key); _SEEN_ORDER.append(key); _METRICS["unique_states"] += 1
        return True


def _record_block(stage, symbol, extra=None):
    with _LOCK:
        _METRICS["shadow_blocks"][stage] += 1; _METRICS["symbols"][symbol] += 1
        if extra:
            _METRICS["last"].append(extra); _METRICS["last"] = _METRICS["last"][-30:]


def _ga(kl):
    return ([float(k["c"]) for k in kl], [float(k["h"]) for k in kl],
            [float(k["l"]) for k in kl], [float(k["o"]) for k in kl],
            [float(k.get("v", 0) or 0) for k in kl])


def _get_atr(h, l, c):
    a = atr(h, l, c)
    return float(a[-1]), float(np.mean(a[-20:])) if len(a) >= 20 else float(a[-1])


def _nexus_reason(dec):
    try:
        if dec.execution_allowed is True: return "approved"
        reasoning = getattr(dec, "reasoning", None) or []
        if reasoning: return str(reasoning[-1])[:160]
        warnings = getattr(dec, "warnings", None) or []
        if warnings: return str(warnings[-1])[:160]
        return str(getattr(dec, "decision", "WAIT"))
    except Exception:
        return "unknown"


def _emit(tag, payload):
    try:
        log.info("[%s] %s", tag, json.dumps(payload, sort_keys=True, separators=(",", ":")))
    except Exception:
        pass


def _register_shadow_state(payload):
    with _LOCK:
        if len(_ACTIVE) >= _MAX_ACTIVE:
            oldest = min(_ACTIVE, key=lambda sid: _ACTIVE[sid].get("opened_at", 0.0))
            _ACTIVE.pop(oldest, None); _METRICS["outcomes"]["EVICTED"] += 1
        _ACTIVE[payload["state_id"]] = payload
    _emit("MTF_SHADOW_EVENT", payload)


def _finalize_outcome(state_id, state, outcome, close_price, bar_ts):
    entry = float(state["entry"]); sl = float(state["sl"]); risk_abs = abs(entry-sl)
    side = state["direction"]
    exit_price = float(state["tp"]) if outcome == "TP" else sl if outcome == "SL" else float(close_price)
    signed_move = (exit_price-entry) if side == "LONG" else (entry-exit_price)
    r_multiple = signed_move/risk_abs if risk_abs > 0 else 0.0
    gross_pct = signed_move/entry*100 if entry else 0.0
    net_pct = gross_pct - (TOTAL_COST*100)
    resolved = outcome in {"TP", "SL", "TIMEOUT"}
    payload = {
        "state_id": state_id, "symbol": state["symbol"], "direction": side,
        "nexus_approved": state["nexus_approved"], "nexus_reason": state["nexus_reason"],
        "outcome": outcome, "entry": entry, "sl": sl, "tp": float(state["tp"]),
        "exit": round(exit_price,10), "mfe_pct": round(state["mfe_pct"],5),
        "mae_pct": round(state["mae_pct"],5), "r_multiple": round(r_multiple,4),
        "gross_pct": round(gross_pct,5), "net_pct_after_cost_model": round(net_pct,5),
        "bars_observed": state["bars_observed"], "opened_bar_ts": state["opened_bar_ts"],
        "resolved_bar_ts": bar_ts, "execution_effect": "NONE",
    }
    with _LOCK:
        _ACTIVE.pop(state_id, None); _METRICS["outcomes"][outcome] += 1
        if resolved:
            _METRICS["resolved_outcomes"] += 1; _METRICS["resolved_r_sum"] += r_multiple
            _METRICS["resolved_net_pct_sum"] += net_pct; _METRICS["mfe_pct_sum"] += state["mfe_pct"]
            _METRICS["mae_pct_sum"] += state["mae_pct"]
        _METRICS["last_outcomes"].append(payload); _METRICS["last_outcomes"] = _METRICS["last_outcomes"][-30:]
    _emit("MTF_SHADOW_OUTCOME", payload)


def _update_shadow_outcomes(symbol, k15):
    bar = _closed_bar(k15)
    if not bar: return
    try:
        bar_ts = bar.get("ts") or bar.get("time") or bar.get("timestamp") or (-2 if len(k15)>1 else -1)
        hi, lo, close = float(bar["h"]), float(bar["l"]), float(bar["c"])
    except Exception:
        return
    with _LOCK:
        active_ids = [sid for sid, st in _ACTIVE.items() if st["symbol"] == symbol]
    for sid in active_ids:
        with _LOCK:
            state = _ACTIVE.get(sid)
            if not state or state.get("last_bar_ts") == bar_ts: continue
            if state.get("opened_bar_ts") == bar_ts:
                state["last_bar_ts"] = bar_ts; continue
            state["last_bar_ts"] = bar_ts; state["bars_observed"] += 1
            entry, sl, tp = float(state["entry"]), float(state["sl"]), float(state["tp"])
            if state["direction"] == "LONG":
                favorable, adverse, hit_tp, hit_sl = max(0.0,hi-entry), max(0.0,entry-lo), hi>=tp, lo<=sl
            else:
                favorable, adverse, hit_tp, hit_sl = max(0.0,entry-lo), max(0.0,hi-entry), lo<=tp, hi>=sl
            state["mfe_pct"] = max(state["mfe_pct"], favorable/entry*100 if entry else 0.0)
            state["mae_pct"] = max(state["mae_pct"], adverse/entry*100 if entry else 0.0)
            bars = state["bars_observed"]
        if hit_tp and hit_sl: _finalize_outcome(sid, state, "AMBIGUOUS_BOTH", close, bar_ts)
        elif hit_tp: _finalize_outcome(sid, state, "TP", close, bar_ts)
        elif hit_sl: _finalize_outcome(sid, state, "SL", close, bar_ts)
        elif bars >= _MAX_OUTCOME_BARS: _finalize_outcome(sid, state, "TIMEOUT", close, bar_ts)


def observe(symbol, k15, k1h, k4h, production_result=None, min_score=60, fee_mult=2.0, vol_mult=1.0):
    """Observe one analyzer call. Never changes or returns production decisions."""
    _update_shadow_outcomes(symbol, k15)
    key = _unique_key(symbol, k15, k1h, k4h)
    if not _remember_unique(key): return
    sid = _state_id(key)
    with _LOCK:
        _METRICS["policy_a_signals" if production_result is not None else "policy_a_holds"] += 1
    if production_result is not None: return
    try:
        if len(k4h)<10 or len(k1h)<15 or len(k15)<20: _record_block("DATA",symbol); return
        c4h,h4h,l4h,o4h,v4h = _ga(k4h[:-1] if len(k4h)>10 else k4h)
        c1h,h1h,l1h,o1h,v1h = _ga(k1h[:-1] if len(k1h)>15 else k1h)
        c15,h15,l15,o15,v15 = _ga(k15[:-1] if len(k15)>20 else k15); price=c15[-1]
        atr_4h,avg_4h=_get_atr(h4h,l4h,c4h); atr_1h,avg_1h=_get_atr(h1h,l1h,c1h); atr_15,avg_15=_get_atr(h15,l15,c15)
        regime=detect_regime(c4h,h4h,l4h,atr_4h)
        if regime in ("COMPRESSED","RANGING","CHOPPY"): _record_block("REGIME_4H",symbol); return
        e20_4h=float(ema(c4h,20)[-1]); e50_4h=float(ema(c4h,50)[-1]); e20_1h=float(ema(c1h,20)[-1]); e50_1h=float(ema(c1h,50)[-1])
        bull4=e20_4h>e50_4h and c4h[-1]>e20_4h; bear4=e20_4h<e50_4h and c4h[-1]<e20_4h
        bull1=e20_1h>e50_1h and c1h[-1]>e20_1h; bear1=e20_1h<e50_1h and c1h[-1]<e20_1h
        if bull4:
            if bear1:
                with _LOCK: _METRICS["blocked_1h_opposite"] += 1
                _record_block("MTF_1H_OPPOSITE",symbol); return
            if bull1: return
            direction="LONG"
        elif bear4:
            if bull1:
                with _LOCK: _METRICS["blocked_1h_opposite"] += 1
                _record_block("MTF_1H_OPPOSITE",symbol); return
            if bear1: return
            direction="SHORT"
        else: _record_block("MTF_4H_NEUTRAL",symbol); return
        with _LOCK: _METRICS["eligible_4h_dir_1h_neutral"] += 1
        s4=score_tf(c4h,h4h,l4h,o4h,v4h,direction,atr_4h,avg_4h); s1=score_tf(c1h,h1h,l1h,o1h,v1h,direction,atr_1h,avg_1h); s15=score_tf(c15,h15,l15,o15,v15,direction,atr_15,avg_15)
        if not s4.get("ok") or not s1.get("ok") or not s15.get("ok"): _record_block("SCORE_INVALID",symbol); return
        combined=round(s4["total"]*.25+s1["total"]*.30+s15["total"]*.45)
        if combined<min_score: _record_block("SCORE",symbol); return
        if s15["rsi_v"]>92 or s15["rsi_v"]<8: _record_block("RSI",symbol); return
        if s15["vol_r"]<.40: _record_block("VOLUME",symbol); return
        if not s15["aligned"] and regime not in ("TRENDING_UP","TRENDING_DOWN"): _record_block("ALIGN_15M",symbol); return
        entry_ok,entry_type=detect_entry(c15,h15,l15,o15,v15,direction,atr_15)
        if not entry_ok:
            combined=max(0,combined-5)
            if combined<min_score: _record_block("ENTRY_TRIGGER",symbol); return
        if entry_type=="BOS_BREAK": sl_mult,tp_mult=1.2,3.6
        elif entry_type=="MOMENTUM": sl_mult,tp_mult=1.5,3.0
        else: sl_mult,tp_mult=2.0,4.0
        sl_atr=max(atr_15,atr_1h*.5)
        if direction=="LONG": sl=round(price-sl_atr*sl_mult,6); tp=round(price+sl_atr*tp_mult,6)
        else: sl=round(price+sl_atr*sl_mult,6); tp=round(price-sl_atr*tp_mult,6)
        rr=abs(tp-price)/abs(sl-price) if abs(sl-price)>0 else 0
        if rr<cfg.MIN_RR_RATIO: _record_block("RR",symbol); return
        cost_pct=TOTAL_COST*100; move_to_tp=abs(tp-price)/price*100
        if move_to_tp<cost_pct*fee_mult: _record_block("COSTS",symbol); return
        with _LOCK: _METRICS["shadow_pre_ai_survivors"] += 1
        dec=nexus_ai.decide(symbol=symbol,k15=k15,k1h=k1h,k4h=k4h,entry=price,sl=sl,tp=tp,min_score=float(os.environ.get("NEXUS_MIN_SCORE","55")))
        approved=getattr(dec,"execution_allowed",False) is True; reason=_nexus_reason(dec)
        payload={"state_id":sid,"ts":time.time(),"opened_bar_ts":_closed_ts(k15),"symbol":symbol,"direction":direction,"regime":regime,
                 "score":combined,"score_4h":s4.get("total"),"score_1h":s1.get("total"),"score_15m":s15.get("total"),
                 "rsi_15m":round(float(s15.get("rsi_v",0)),4),"volume_ratio_15m":round(float(s15.get("vol_r",0)),4),"aligned_15m":bool(s15.get("aligned")),
                 "entry_type":entry_type,"entry":round(float(price),10),"sl":round(float(sl),10),"tp":round(float(tp),10),"rr_gross":round(float(rr),4),
                 "cost_pct_model":round(float(cost_pct),5),"move_to_tp_pct":round(float(move_to_tp),5),"nexus_approved":approved,"nexus_reason":reason,
                 "mfe_pct":0.0,"mae_pct":0.0,"bars_observed":0,"last_bar_ts":_closed_ts(k15),"opened_at":time.time(),"execution_effect":"NONE"}
        with _LOCK:
            _METRICS["shadow_nexus_approved" if approved else "shadow_nexus_vetoed"] += 1
            if not approved: _METRICS["nexus_reasons"][reason] += 1
            _METRICS["symbols"][symbol] += 1; _METRICS["last"].append(dict(payload)); _METRICS["last"]=_METRICS["last"][-30:]
        _register_shadow_state(payload)
    except Exception as exc:
        _record_block("SHADOW_ERROR",symbol,{"ts":time.time(),"symbol":symbol,"error":type(exc).__name__})


def snapshot():
    with _LOCK:
        unique=_METRICS["unique_states"]; eligible=_METRICS["eligible_4h_dir_1h_neutral"]; survivors=_METRICS["shadow_pre_ai_survivors"]
        nx_total=_METRICS["shadow_nexus_approved"]+_METRICS["shadow_nexus_vetoed"]; resolved=_METRICS["resolved_outcomes"]
        return {"mode":"SHADOW_ONLY","policy_a":"4H+1H explicit alignment","policy_b":"4H directional + 1H neutral allowed; opposite blocked",
                "started_at":_METRICS["started_at"],"uptime_minutes":round((time.time()-_METRICS["started_at"])/60,1),"raw_calls":_METRICS["raw_calls"],
                "unique_states":unique,"policy_a_signals":_METRICS["policy_a_signals"],"policy_a_holds":_METRICS["policy_a_holds"],
                "eligible_4h_dir_1h_neutral":eligible,"eligible_pct":round(eligible/unique*100,2) if unique else 0.0,"blocked_1h_opposite":_METRICS["blocked_1h_opposite"],
                "shadow_pre_ai_survivors":survivors,"survival_pct_of_eligible":round(survivors/eligible*100,2) if eligible else 0.0,
                "shadow_nexus_approved":_METRICS["shadow_nexus_approved"],"shadow_nexus_vetoed":_METRICS["shadow_nexus_vetoed"],
                "nexus_approval_pct":round(_METRICS["shadow_nexus_approved"]/nx_total*100,2) if nx_total else 0.0,"shadow_blocks":_METRICS["shadow_blocks"].most_common(),
                "nexus_reasons":_METRICS["nexus_reasons"].most_common(10),"symbols":_METRICS["symbols"].most_common(12),"active_shadow_states":len(_ACTIVE),
                "outcomes":_METRICS["outcomes"].most_common(),"resolved_outcomes":resolved,"shadow_expectancy_r":round(_METRICS["resolved_r_sum"]/resolved,4) if resolved else None,
                "shadow_expectancy_net_pct":round(_METRICS["resolved_net_pct_sum"]/resolved,5) if resolved else None,"avg_mfe_pct":round(_METRICS["mfe_pct_sum"]/resolved,5) if resolved else None,
                "avg_mae_pct":round(_METRICS["mae_pct_sum"]/resolved,5) if resolved else None,"last":list(_METRICS["last"][-12:]),"last_outcomes":list(_METRICS["last_outcomes"][-12:]),
                "execution_effect":"NONE"}
