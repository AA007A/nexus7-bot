"""Forward evidence collector + production-parity outcome resolver.

Every AI_RUNTIME_HOOK_POPULATION_V1 candidate the isolated SHADOW observer
sees is persisted ONCE (deterministic ``candidate_id``), whether the frozen
policy said TRADE or ABSTAIN. Each PENDING candidate is later followed on
CLOSED candles only, with the SAME production-parity exit/cost functions the
replay uses (nexus_oos_real_replay._parity_outcome ->
nexus_oos_execution_parity.simulate_production_exit + legs_net_r: native
SL/TP, 1R partial, break-even, trailing, 2R, fees, slippage, funding).

Predeclared rules (fixed before any forward data):
  * DECISION_BAR_MISSING  the decision 15m candle never appears -> INVALID;
  * GAP_RULE              a missing 15m candle in the outcome path before the
                          exit -> RIGHT_CENSORED_DATA_GAP (never interpolated);
  * NO FORCED CLOSE       restarts, day boundaries and deadlines never close a
                          candidate; still open at window end ->
                          RIGHT_CENSORED_DATA_END (excluded from resolved R);
  * IDEMPOTENT            a final row is never rewritten; resolving twice is a no-op.
"""
from __future__ import annotations

import hashlib
import json

from bot.ai import evidence_store as es

M15 = 15 * 60_000
COLLECTOR_VERSION = "AI_FORWARD_COLLECTOR_V1"
GAP_RULE = "MISSING_15M_CANDLE_BEFORE_EXIT => RIGHT_CENSORED_DATA_GAP; DECISION_BAR_MISSING => INVALID"


def _ts(c) -> int:
    t = int(c.get("ts", 0) or 0)
    return t * 1000 if t < 100_000_000_000 else t


def candidate_id(*, obs, bundle_sha256: str, policy_sha256: str, code_sha: str | None) -> str:
    """One deterministic identity per hook observation and frozen identity."""
    body = json.dumps({"population": obs.population, "profile": obs.profile, "symbol": obs.symbol,
                       "event_ts": int(obs.decision_ts), "direction": obs.direction,
                       "bundle": bundle_sha256, "policy": policy_sha256, "code": code_sha}, sort_keys=True)
    return hashlib.sha256(body.encode()).hexdigest()


def candidate_record(*, obs, decision, signal_tp: float, final_tp: float, costs: dict, regime: str,
                     code_sha: str | None, bundle, fee_rate: float, slippage_rate: float,
                     now_ms: int) -> dict:
    """Everything needed to evaluate the candidate later (no weights, no secrets)."""
    d = decision.to_dict()
    cb = d.get("cost_breakdown") or {}
    return {
        "collector_version": COLLECTOR_VERSION,
        "candidate_id": candidate_id(obs=obs, bundle_sha256=bundle.sha256, policy_sha256=bundle.policy.sha256,
                                     code_sha=code_sha),
        "decision_id": d["decision_id"], "event_ts": int(obs.decision_ts), "symbol": obs.symbol,
        "direction": obs.direction, "code_sha": code_sha, "bundle_sha256": bundle.sha256,
        "policy_sha256": bundle.policy.sha256, "feature_schema_sha256": d["feature_schema_sha256"],
        "hook_population": obs.population, "hook_profile": obs.profile,
        "entry": obs.entry, "signal_stop": obs.stop, "signal_tp": float(signal_tp), "final_tp": float(final_tp),
        "planned_rr": obs.rr, "strategy_score": obs.strategy_score, "nexus_confidence": obs.nexus_confidence,
        "regime": regime, "feature_hash": d["feature_hash"],
        "probability_raw": d["probability_raw"], "probability_calibrated": d["probability_calibrated"],
        "predicted_gross_r": d["expected_r"], "predicted_net_r": d["expected_net_r_after_costs"],
        "ai_decision": "TRADE" if decision.is_trade else "ABSTAIN",
        "vetoes": list(d["vetoes"]), "reason_codes": list(d["reason_codes"]),
        "decision_latency_ms": d["decision_latency_ms"],
        "assumptions": {"fee_rate": float(fee_rate), "slippage_rate": float(slippage_rate),
                        "fees_r": costs.get("fees_r"), "slippage_buffer_r": costs.get("slippage_r"),
                        "funding_r": costs.get("funding_r"), "cost_contract": cb},
        "observed_at_ms": int(now_ms),
    }


def _mfe_mae(bars, i, j, direction, fill, stop):
    risk = abs(fill - stop)
    if risk <= 0 or j < i:
        return None, None
    hi = max(float(b["h"]) for b in bars[i:j + 1])
    lo = min(float(b["l"]) for b in bars[i:j + 1])
    if direction == "LONG":
        return (hi - fill) / risk, (lo - fill) / risk
    return (fill - lo) / risk, (fill - hi) / risk


def resolve_one(rec: dict, bars: list, funding_events: list, *, now_ms: int, exit_policy) -> tuple[str, dict | None]:
    """Evaluate one PENDING candidate on bars CLOSED by ``now_ms``.
    Returns (status, outcome) with status PENDING (keep waiting), RESOLVED,
    RIGHT_CENSORED_DATA_GAP or INVALID."""
    from bot import nexus_oos_real_replay as rp
    from bot.backtest import _timestamp_index
    ev = int(rec["event_ts"])
    closed = sorted({_ts(b): b for b in bars if _ts(b) + M15 <= int(now_ms) and _ts(b) >= ev}.items())
    if not closed:
        return (es.INVALID, {"reason": "DECISION_BAR_MISSING"}) if int(now_ms) >= ev + 4 * M15 else (es.PENDING, None)
    if closed[0][0] != ev:
        return es.INVALID, {"reason": "DECISION_BAR_MISSING"}
    path = [closed[0][1]]
    gap_at = None
    for (t0, _), (t1, b) in zip(closed, closed[1:]):
        if t1 - t0 != M15:
            gap_at = t0 + M15
            break
        path.append(b)
    ts15 = _timestamp_index(path)
    evs = sorted(funding_events or [], key=lambda e: int(e.get("timepoint", 0) or 0))
    pctx = {"k15": path, "ts15": ts15, "funding_events": evs,
            "funding_ts": [int(e.get("timepoint", 0) or 0) for e in evs], "exit_policy": exit_policy}
    kw = dict(direction=rec["direction"], i=0, sig_entry=float(rec["entry"]), sl=float(rec["signal_stop"]),
              tp=float(rec["final_tp"]))
    a = rec["assumptions"]
    out = rp._parity_outcome(pctx, fee_rate=a["fee_rate"], slip=a["slippage_rate"], **kw)
    if out is None:
        return es.INVALID, {"reason": "SIMULATION_UNAVAILABLE"}
    sim = out["sim"]
    if sim["outcome_status"] != "RESOLVED":
        if gap_at is not None:
            return es.CENSORED_GAP, {"reason": "GAP_RULE", "gap_at_ts": gap_at, "rule": GAP_RULE,
                                     "last_closed_ts": path[-1] and _ts(path[-1])}
        return es.PENDING, None
    stress = {}
    for name, (fm, sm) in rp.COST_SCENARIOS.items():
        o = rp._parity_outcome(pctx, fee_rate=a["fee_rate"] * fm, slip=a["slippage_rate"] * sm, **kw)
        stress[name] = rp._realized(o)
    no_slip = rp._realized(rp._parity_outcome(pctx, fee_rate=a["fee_rate"], slip=0.0, **kw))
    j = max(k for k, b in enumerate(path) if _ts(b) < int(sim["exit_ts"]))
    mfe, mae = _mfe_mae(path, 0, j, rec["direction"], float(sim["fill"]), float(rec["signal_stop"]))
    return es.RESOLVED, {
        "outcome_end_ts": int(sim["exit_ts"]), "exit_reason": sim["exit_reason"],
        "gross_r": out.get("gross_r"), "fees_r": out.get("fees_r"), "funding_r": out.get("funding_r"),
        "slippage_r": (float(out["r"]) - no_slip) if no_slip is not None else None,
        "modeled_net_r": float(out["r"]), "mfe_r": mfe, "mae_r": mae,
        "holding_time_ms": int(sim["exit_ts"]) - int(sim["entry_ts"]),
        "legs": [list(x) for x in sim["legs"]], "cost_stress_r": stress,
        "resolved_with_bars_closed_by_ms": int(now_ms)}


async def resolve_pending(store, window_id: str, fetch_bars, fetch_funding, *, now_ms: int, exit_policy) -> dict:
    """One resolver pass over the window's PENDING candidates (idempotent).
    Information is capped at the window's fixed end: bars/funding after
    window_end_ms are never used."""
    w = await store.window(window_id)
    horizon = min(int(now_ms), int(w["window_end_ms"]))
    counts = {"pending": 0, "resolved": 0, "censored_gap": 0, "invalid": 0, "information_horizon_ms": horizon}
    for rec in await store.candidates(window_id, es.PENDING):
        bars = [b for b in await fetch_bars(rec["symbol"], int(rec["event_ts"]), horizon) if _ts(b) + M15 <= horizon]
        funding = [e for e in (await fetch_funding(rec["symbol"], int(rec["event_ts"]), horizon) or [])
                   if int(e.get("timepoint", 0) or 0) <= horizon]
        status, outcome = resolve_one(rec, bars, funding, now_ms=horizon, exit_policy=exit_policy)
        if status == es.PENDING:
            counts["pending"] += 1
            continue
        if await store.finalize(rec["candidate_id"], status, outcome):
            counts[{"RESOLVED": "resolved", es.CENSORED_GAP: "censored_gap", es.INVALID: "invalid"}[status]] += 1
    return counts


async def close_window(store, window_id: str, fetch_bars, fetch_funding, *, exit_policy, now_ms: int) -> dict:
    """At the FIXED end boundary: stop accepting candidates, resolve with
    information up to window_end_ms only, censor what is still open
    (RIGHT_CENSORED_DATA_END; never force-closed), mark the window CLOSED.
    Never starts another window."""
    w = await store.window(window_id)
    if w["status"] == es.W_CLOSED:
        return {"closed": True, "already_closed": True, "window_id": window_id}
    if w["status"] != es.W_ACTIVE:
        raise es.WindowRefused(f"window is {w['status']}")
    end = int(w["window_end_ms"])
    if int(now_ms) < end:
        raise es.WindowRefused("window end not reached; no early stopping")
    res = await resolve_pending(store, window_id, fetch_bars, fetch_funding, now_ms=end, exit_policy=exit_policy)
    n = 0
    for rec in await store.candidates(window_id, es.PENDING):
        if await store.finalize(rec["candidate_id"], es.CENSORED_END,
                                {"reason": "WINDOW_END_OPEN_POSITION", "window_end_ms": end}):
            n += 1
    await store.update_window(window_id, status=es.W_CLOSED, closed_at_ms=end)
    return {"closed": True, "already_closed": False, "window_id": window_id, "window_end_ms": end,
            "resolver": res, "right_censored_data_end": n, "event": "FORWARD_WINDOW_CLOSED"}
