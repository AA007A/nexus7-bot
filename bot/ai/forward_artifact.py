"""build_forward_shadow_artifact: FORWARD_SHADOW_EVIDENCE_V2 artifact from the
durable candidate/outcome rows and observer heartbeats.

Two separate products, never mixed:
  FORWARD_OBSERVATION_DATASET     all hook candidates (TRADE and ABSTAIN) +
                                  resolved outcomes (research dataset).
  FROZEN_POLICY_FORWARD_EVIDENCE  only frozen-policy TRADE candidates; uplift
                                  baseline = HOOK_BASELINE (all resolved hook
                                  candidates), never generic executable rows.

The candidate service may BUILD and diagnose evidence but never self-attests:
its verdict is COLLECTING / INSUFFICIENT_EVIDENCE / BLOCK. PASS for lifecycle
promotion comes only from the protected evaluator.
"""
from __future__ import annotations

import math

from bot.ai import evidence_store as es
from bot.ai import forward_evidence as fe

SCHEMA = "FORWARD_SHADOW_ARTIFACT_V2"
IN_CANDIDATE_VERDICTS = ("COLLECTING", "INSUFFICIENT_EVIDENCE", "BLOCK")
DAY_MS = 86_400_000


def _pct(xs, q):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    return xs[min(len(xs) - 1, int(math.ceil(q * len(xs))) - 1)]


def _bucket(v, edges):
    if v is None:
        return "MISSING"
    for lo, hi in zip(edges, edges[1:]):
        if lo <= v < hi:
            return f"[{lo},{hi})"
    return f"[{edges[-2]},{edges[-1]}]"


def _rows(cands):
    """Resolved outcomes in the inference row format (ts / r / outcome_end_ts)."""
    out = []
    for c in cands:
        if c["status"] != es.RESOLVED:
            continue
        o = c["outcome"]
        out.append({"ts": int(c["event_ts"]), "r": float(o["modeled_net_r"]), "outcome_status": "RESOLVED",
                    "outcome_end_ts": int(o["outcome_end_ts"]), "symbol": c["symbol"],
                    "direction": c["direction"], "regime": c.get("regime"),
                    "trade": c["ai_decision"] == "TRADE", "p": c.get("probability_calibrated"),
                    "pred": c.get("predicted_net_r"),
                    "cost_r": {k: v for k, v in (o.get("cost_stress_r") or {}).items() if v is not None},
                    "week": int(c["event_ts"]) // (7 * DAY_MS)})
    return out


def _mean(rs):
    return (sum(r["r"] for r in rs) / len(rs)) if rs else None


def _segments(rs, key):
    seg = {}
    for r in rs:
        s = seg.setdefault(str(r[key]), {"n": 0, "total_r": 0.0})
        s["n"] += 1
        s["total_r"] += r["r"]
    for s in seg.values():
        s["mean_r"] = s["total_r"] / s["n"]
    return seg


def _share(seg):
    pos = [v["total_r"] for v in seg.values() if v["total_r"] > 0]
    return (max(pos) / sum(pos)) if pos else None


def build_forward_shadow_artifact(candidates: list, heartbeats: list, *, identity: dict, window_start_ms: int,
                                  window_end_ms: int | None, safety: dict, journal_verified: bool,
                                  continuity_broken: bool = False) -> dict:
    from bot import nexus_oos_inference as inf
    from bot import nexus_oos_research as res
    from bot.ai import calibration as cal
    c = fe.SHADOW_CONTRACT
    end = int(window_end_ms) if window_end_ms is not None else max(
        [int(h["ts"]) for h in heartbeats] + [int(window_start_ms)])
    days = max(0.0, (end - int(window_start_ms)) / DAY_MS)
    by_status = {}
    for x in candidates:
        by_status[x["status"]] = by_status.get(x["status"], 0) + 1
    rows = _rows(candidates)
    trades = [r for r in rows if r["trade"]]
    req = inf.required_block_ms(rows) if rows else None
    authority = {}
    if trades and rows:
        authority = {
            "frozen_policy_expectancy": inf.dependence_aware_mean(rows, lambda r: r["trade"], required_ms=req),
            "uplift_vs_hook_baseline": inf.dependence_aware_diff(rows, lambda r: r["trade"], lambda r: True,
                                                                 required_ms=req)}
    labelled = [r for r in rows if r["p"] is not None]
    calib = cal.report([r["p"] for r in labelled], [1.0 if r["r"] > 0 else 0.0 for r in labelled]) \
        if len(labelled) >= 2 else None
    lat = [x.get("decision_latency_ms") for x in candidates]
    scans = [h for h in heartbeats if h.get("kind") == "SCAN"]
    expected_scans = int(days * 96)
    gaps = [h for h in heartbeats if h.get("kind") == "GAP"]
    identities = {k: identity.get(k) for k in ("code_sha", "bundle_sha256", "policy_sha256",
                                                "feature_schema_sha256", "hook_population", "hook_profile")}
    mixed = sorted({(x.get("bundle_sha256"), x.get("policy_sha256"), x.get("code_sha")) for x in candidates})
    zero_order = (safety.get("exchange_credentials_present") is False
                  and safety.get("mutating_client_methods") is False
                  and safety.get("execution_lease_acquired") is False and safety.get("orders_sent") == 0)
    blockers = []
    if not zero_order:
        blockers.append("ZERO_ORDER_ASSERTION_FAILED")
    if not journal_verified:
        blockers.append("JOURNAL_NOT_VERIFIED")
    if continuity_broken:
        blockers.append("EVIDENCE_CONTINUITY_BROKEN")
    if len(mixed) > 1:
        blockers.append("IDENTITY_CHANGED_MID_WINDOW")
    if set(identity.get("symbols") or []) != set(fe.SHADOW_UNIVERSE):
        blockers.append("SYMBOL_UNIVERSE_NOT_PINNED")
    insufficient = []
    if days < c["min_calendar_days"]:
        insufficient.append("WINDOW_SHORTER_THAN_CONTRACT")
    if len(rows) < c["min_hook_candidates_resolved"]:
        insufficient.append("TOO_FEW_RESOLVED_HOOK_CANDIDATES")
    if len(trades) < c["min_policy_trades_resolved"]:
        insufficient.append("TOO_FEW_FROZEN_POLICY_TRADES")
    if len({r["symbol"] for r in trades}) < c["min_symbols_with_policy_trades"]:
        insufficient.append("TOO_FEW_SYMBOLS_WITH_POLICY_TRADES")
    verdict = "BLOCK" if blockers else "COLLECTING" if window_end_ms is None else "INSUFFICIENT_EVIDENCE"
    # In-candidate evaluation never returns PASS: even a complete window is
    # INSUFFICIENT_EVIDENCE here until the protected evaluator recomputes it.
    assert verdict in IN_CANDIDATE_VERDICTS
    return {
        "schema": SCHEMA, "contract": c["name"], "contract_sha256": fe.CONTRACT_SHA256["SHADOW"],
        **identities, "symbols": sorted(identity.get("symbols") or []),
        "window": {"start_ms": int(window_start_ms), "end_ms": end, "closed": window_end_ms is not None,
                   "calendar_days": days},
        "counts": {"candidates": len(candidates), "ai_decisions": len(candidates),
                   "trade": sum(1 for x in candidates if x["ai_decision"] == "TRADE"),
                   "abstain": sum(1 for x in candidates if x["ai_decision"] == "ABSTAIN"),
                   "by_status": by_status, "resolved": by_status.get(es.RESOLVED, 0),
                   "censored": by_status.get(es.CENSORED_END, 0) + by_status.get(es.CENSORED_GAP, 0),
                   "invalid_or_data_gap": by_status.get(es.INVALID, 0) + by_status.get(es.CENSORED_GAP, 0),
                   "pending": by_status.get(es.PENDING, 0)},
        "segments_present": {"symbols": sorted({x["symbol"] for x in candidates}),
                             "regimes": sorted({str(x.get("regime")) for x in candidates})},
        "FORWARD_OBSERVATION_DATASET": {
            "resolved_hook_candidates": len(rows), "hook_baseline_mean_r": _mean(rows),
            "calibration": calib,
            "diagnostics_predeclared": {
                "probability": _segments([dict(r, b=_bucket(r["p"], fe.PROBABILITY_BUCKET_EDGES))
                                          for r in rows], "b"),
                "predicted_net_r": _segments([dict(r, b=_bucket(r["pred"], fe.PREDICTED_NET_R_BUCKET_EDGES))
                                              for r in rows], "b"),
                "direction": _segments(rows, "direction"), "regime": _segments(rows, "regime"),
                "symbol": _segments(rows, "symbol"),
                "role": "DIAGNOSTIC_ONLY_NOT_A_POLICY"}},
        "FROZEN_POLICY_FORWARD_EVIDENCE": {
            "status": "INSUFFICIENT_EVIDENCE" if not trades else "MEASURED",
            "trades_resolved": len(trades), "frozen_policy_mean_r": _mean(trades),
            "hook_baseline_mean_r": _mean(rows),
            "uplift_r": (_mean(trades) - _mean(rows)) if trades and rows else None,
            "baseline": "HOOK_BASELINE", "authority": authority,
            "cost_stress": res.cost_stress(trades) if trades else None,
            "concentration": {"symbol_share_of_positive_r": _share(_segments(trades, "symbol")) if trades else None,
                              "week_share_of_positive_r": _share(_segments(trades, "week")) if trades else None}},
        "runtime": {"decision_latency_ms": {"p50": _pct(lat, 0.5), "p95": _pct(lat, 0.95), "p99": _pct(lat, 0.99)},
                    "scans": len(scans), "expected_scans": expected_scans,
                    "uptime_fraction": (len(scans) / expected_scans) if expected_scans else None,
                    "data_gaps": len(gaps)},
        "safety": dict(safety), "zero_order_verified": zero_order, "journal_verified": bool(journal_verified),
        "evidence_continuity_broken": bool(continuity_broken),
        "blockers": sorted(set(blockers)), "insufficient": sorted(set(insufficient)),
        "verdict": verdict, "final_authority": "PROTECTED_EVALUATOR_ONLY",
    }
