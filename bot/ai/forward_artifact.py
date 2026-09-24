"""FORWARD_SHADOW_EVIDENCE_V3 artifact, derived ONLY from the durable evidence DB.

    build_forward_shadow_artifact_from_store(store)          (library)
    python -m bot.ai.forward_artifact --from-evidence-db --output PATH   (read-only export)

Window bounds, identity, contract, symbol universe, continuity, candidates,
boundary journal, heartbeats and coverage all come from the store; nothing
(bounds, journal_verified, safety) can be supplied by the caller. Every row
digest, the window identity, boundary uniqueness and heartbeat/window
binding are re-verified here.

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

SCHEMA = "FORWARD_SHADOW_ARTIFACT_V3"
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


def _build_forward_shadow_artifact(candidates: list, heartbeats: list, *, identity: dict, window_start_ms: int,
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


# ── V3: everything derived from the durable store ───────────────────────────
def _coverage(window: dict, boundaries: list) -> dict:
    universe = list(window["identity"]["symbol_universe"])
    start, end = int(window["window_start_ms"]), int(window["window_end_ms"])
    expected_b = (end - start) // fe.M15_MS
    expected_sb = expected_b * len(universe)
    by = {}
    for r in boundaries:
        by[r["status"]] = by.get(r["status"], 0) + 1
    last = window.get("last_completed_boundary_ms")
    elapsed_b = 0 if last is None else (int(last) - start) // fe.M15_MS + 1
    seen = {(int(r["boundary_ms"]), r["symbol"]) for r in boundaries}
    unaccounted_elapsed = sum(1 for k in range(elapsed_b) for sym in universe
                              if (start + k * fe.M15_MS, sym) not in seen)
    processed = sum(by.get(x, 0) for x in fe.PROCESSED_STATUSES)
    errors = by.get("ERROR", 0)
    complete_boundaries = sum(1 for k in range(elapsed_b)
                              if all((start + k * fe.M15_MS, sym) in seen for sym in universe))
    return {"expected_boundaries": expected_b, "expected_symbol_boundaries": expected_sb,
            "elapsed_boundaries": elapsed_b, "completed_boundaries": int(window.get("completed_boundaries") or 0),
            "fully_journaled_boundaries": complete_boundaries,
            "journaled_symbol_boundaries": len(boundaries), "by_status": dict(sorted(by.items())),
            "complete_symbol_boundaries": processed, "missing_symbol_boundaries":
                by.get("DATA_MISSING", 0) + by.get("MISSED", 0), "error_symbol_boundaries": errors,
            "unaccounted_elapsed_symbol_boundaries": unaccounted_elapsed,
            "unaccounted_symbol_boundaries": expected_sb - len(boundaries),
            "coverage_fraction": processed / expected_sb if expected_sb else None,
            "coverage_fraction_elapsed": processed / (elapsed_b * len(universe)) if elapsed_b else None,
            "error_fraction": errors / expected_sb if expected_sb else None}


def _completeness(cov: dict, *, closed: bool, broken: bool) -> dict:
    rule = fe.COMPLETENESS_RULE
    fails = []
    if broken:
        fails.append("CONTINUITY_BROKEN")
    if cov["unaccounted_elapsed_symbol_boundaries"] > 0:
        fails.append("UNACCOUNTED_SYMBOL_BOUNDARIES")
    if closed:
        if cov["unaccounted_symbol_boundaries"] > rule["max_unaccounted_symbol_boundaries"]:
            fails.append("UNACCOUNTED_SYMBOL_BOUNDARIES_AT_END")
        if (cov["coverage_fraction"] or 0.0) < rule["min_processed_symbol_boundary_fraction"]:
            fails.append("PROCESSED_COVERAGE_BELOW_RULE")
        if (cov["error_fraction"] or 0.0) > rule["max_error_symbol_boundary_fraction"]:
            fails.append("ERROR_FRACTION_ABOVE_RULE")
    return {"rule": rule, "evaluated_at_window_end": closed, "failures": sorted(set(fails)),
            "satisfied": (not fails) if closed else (None if not fails else False)}


async def build_forward_shadow_artifact_from_store(store, *, window_id: str | None = None) -> dict:
    """The only public builder. Bounds/identity/journal status are DERIVED."""
    from bot.ai.runtime import JournalIntegrityError
    integrity = []
    try:
        windows = await store.windows()                       # every window digest + identity verified
    except JournalIntegrityError as exc:
        return {"schema": SCHEMA, "verdict": "BLOCK", "blockers": ["JOURNAL_NOT_VERIFIED"],
                "integrity_errors": [str(exc)], "final_authority": "PROTECTED_EVALUATOR_ONLY"}
    if window_id is None:
        act = [w for w in windows if w["status"] == es.W_ACTIVE]
        pick = act or sorted(windows, key=lambda w: w["window_start_ms"])[-1:]
    else:
        pick = [w for w in windows if w["window_id"] == window_id]
    if not pick:
        return {"schema": SCHEMA, "verdict": "INSUFFICIENT_EVIDENCE", "reason": "NO_EVIDENCE_WINDOW",
                "blockers": [], "final_authority": "PROTECTED_EVALUATOR_ONLY"}
    w = pick[0]
    wid = w["window_id"]
    ident = w["identity"]
    cands, bnds, hbs = [], [], []
    try:
        cands = await store.candidates(wid)
        bnds = await store.boundaries(wid)
        hbs = await store.heartbeats(wid)
    except JournalIntegrityError as exc:
        integrity.append(str(exc))
    # identity / binding / uniqueness re-verification (computed, never supplied)
    for c in cands:
        if any(c.get(k) != ident.get(v) for k, v in es.CANDIDATE_IDENTITY_FIELDS.items()):
            integrity.append(f"candidate {c['candidate_id']} identity differs from window")
        if not (w["window_start_ms"] <= int(c["event_ts"]) < w["window_end_ms"]):
            integrity.append(f"candidate {c['candidate_id']} outside window bounds")
    keys = [(int(b["boundary_ms"]), b["symbol"]) for b in bnds]
    if len(keys) != len(set(keys)):
        integrity.append("duplicate (boundary, symbol) rows")
    for b in bnds:
        if b["symbol"] not in ident["symbol_universe"] or \
                (int(b["boundary_ms"]) - w["window_start_ms"]) % fe.M15_MS or \
                not (w["window_start_ms"] <= int(b["boundary_ms"]) < w["window_end_ms"]):
            integrity.append(f"boundary row {b['boundary_id']} not canonical for the window")
    for h in hbs:
        if h.get("window_id") != wid:
            integrity.append("heartbeat not bound to window")
    journal_verified = not integrity
    safeties = [h.get("safety") for h in hbs if h.get("kind") == "SCAN" and h.get("safety")]
    zero_violation = any(not (s.get("exchange_credentials_present") is False and
                              s.get("mutating_client_methods") is False and
                              s.get("execution_lease_acquired") is False and s.get("orders_sent") == 0)
                         for s in safeties)
    safety = {"exchange_credentials_present": False, "mutating_client_methods": False,
              "execution_lease_acquired": False, "orders_sent": 0} if not zero_violation else \
        {"orders_sent": None, "violation": True}
    closed = w["status"] == es.W_CLOSED
    broken = bool(w.get("continuity_broken"))
    body = _build_forward_shadow_artifact(
        cands, hbs, identity={**ident, "symbols": ident["symbol_universe"]},
        window_start_ms=w["window_start_ms"], window_end_ms=w["window_end_ms"] if closed else None,
        safety=safety, journal_verified=journal_verified, continuity_broken=broken)
    cov = _coverage(w, bnds)
    comp = _completeness(cov, closed=closed, broken=broken)
    blockers = set(body["blockers"])
    if w["status"] == es.W_INVALID:
        blockers.add("WINDOW_INVALID_IDENTITY_CHANGE")
    if ident.get("symbol_universe") != list(fe.SHADOW_UNIVERSE):
        blockers.add("SYMBOL_UNIVERSE_NOT_PINNED_IN_ORDER")
    if ident.get("contract_name") != fe.SHADOW_CONTRACT["name"] or \
            ident.get("contract_sha256") != fe.CONTRACT_SHA256["SHADOW"]:
        blockers.add("CONTRACT_MISMATCH")
    obs_valid = journal_verified and not ({"IDENTITY_CHANGED_MID_WINDOW", "WINDOW_INVALID_IDENTITY_CHANGE",
                                           "CONTRACT_MISMATCH"} & blockers)
    policy_status = ("INVALID_CONTINUITY" if broken else "INVALID_COVERAGE" if comp["satisfied"] is False
                     else "PENDING_WINDOW_OPEN" if not closed else "VALID" if obs_valid else "INVALID_JOURNAL")
    if closed and comp["satisfied"] is False:
        blockers.add("COMPLETENESS_RULE_FAILED")
    fpe = dict(body["FROZEN_POLICY_FORWARD_EVIDENCE"])
    if policy_status not in ("VALID", "PENDING_WINDOW_OPEN"):
        fpe["status"] = "DISQUALIFIED_" + policy_status
        fpe["authority"] = {}
    verdict = "BLOCK" if blockers else "INSUFFICIENT_EVIDENCE" if closed else "COLLECTING"
    assert verdict in IN_CANDIDATE_VERDICTS
    return {**body, "FROZEN_POLICY_FORWARD_EVIDENCE": fpe,
            "window_id": wid, "window_identity": ident, "window_identity_sha256": w["identity_sha256"],
            "window": {"start_ms": w["window_start_ms"], "end_ms": w["window_end_ms"], "status": w["status"],
                       "closed": closed, "closed_at_ms": w.get("closed_at_ms"),
                       "fixed_duration_ms": w["window_end_ms"] - w["window_start_ms"],
                       "start_source": "DURABLE_WINDOW_ROW", "end_source": "DURABLE_WINDOW_ROW",
                       "calendar_days": body["window"]["calendar_days"]},
            "symbols": list(ident["symbol_universe"]),
            "continuity": {"continuity_broken": broken, "continuity_reason": w.get("continuity_reason"),
                           "source": "DURABLE_WINDOW_ROW"},
            "coverage": cov, "completeness": comp,
            "OBSERVATION_DATASET_VALIDITY": {"journal_verified": journal_verified, "integrity_errors": integrity,
                                             "valid": obs_valid},
            "FROZEN_POLICY_EVIDENCE_VALIDITY": {"status": policy_status, "completeness_satisfied": comp["satisfied"],
                                                "continuity_broken": broken,
                                                "valid": policy_status == "VALID"},
            "journal_verified": journal_verified, "journal_verified_source": "COMPUTED_FROM_STORE",
            "evidence_db": {"authority_id": (store.identity or {}).get("authority_id"),
                            "fingerprint": (store.identity or {}).get("fingerprint")},
            "superseded_contracts": fe.SUPERSEDED,
            "blockers": sorted(blockers), "verdict": verdict}


_FORBIDDEN_OUTPUT = ("://", "password", "EVIDENCE_DATABASE_URL", "KUCOIN_API")


def sanitized_json(art: dict) -> str:
    import json
    text = json.dumps(art, sort_keys=True, indent=2, default=str) + "\n"
    low = text.lower()
    bad = [t for t in _FORBIDDEN_OUTPUT if t.lower() in low]
    if bad:
        raise ValueError(f"artifact not sanitized: {bad}")
    return text


async def _export(env, output, window_id=None) -> dict:
    from pathlib import Path
    store = await es.EvidenceStore.connect(env, read_only=True)
    try:
        art = await build_forward_shadow_artifact_from_store(store, window_id=window_id)
    finally:
        await store.conn.close()
    p = Path(output)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(sanitized_json(art), encoding="utf-8")
    return art


def main(argv=None) -> int:
    import argparse
    import asyncio
    import os
    ap = argparse.ArgumentParser(description="Read-only FORWARD_SHADOW_EVIDENCE_V3 export")
    ap.add_argument("--from-evidence-db", action="store_true", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--window-id")
    a = ap.parse_args(argv)
    art = asyncio.run(_export(dict(os.environ), a.output, a.window_id))
    print(f"verdict={art['verdict']} window_id={art.get('window_id')} output={a.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
