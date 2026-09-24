"""AI_RESEARCH_PROMOTION_GATE — independent verifier of the AI meta-model
evidence in a replay artifact. Fail closed; never trusts self-reported
authority_status / authority_ci_* / stored ACF / residual verdicts.

It recomputes, from the retained compact per-block aggregates
(AI_AGGREGATE_BLOCK_BOOTSTRAP_V1: every bootstrap draw rebuilt from block
sums/counts with the predeclared seed/sample count; influence-score residual
ACF; >= 30 resampling blocks, >= 20 calendar pairs, zero variance fails):
  * AI-approved expectancy authority (block CIs at lengths >= the horizon the
    gate re-derives from the replay's own resolved-horizon statistics);
  * AI uplift vs the executable baseline authority.
It also checks purged window ordering, sample adequacy, symbol / period
concentration, cost stress, calibration vs the base rate on every TEST fold,
and the per-fold AI portfolio replay. A PASS is research evidence on
previously inspected history only; it can create a SHADOW_CHALLENGER, never
a LIVE champion.
"""
from __future__ import annotations

from bot import nexus_oos_inference as inf
from bot import nexus_oos_promotion_gate as g
from bot.ai import aggregate_bootstrap as agg
from bot.ai import hook as ai_hook
from bot.ai import training as tr

GATE = "AI_RESEARCH_PROMOTION_GATE"
DATA_LABEL = "HISTORICAL_OOS_PREVIOUSLY_INSPECTED"
COST_SCENARIOS_REQUIRED = ("fees_plus_50pct", "slippage_x2", "combined_adverse")


def _share(segments: dict):
    pos = {k: v.get("total_r", 0.0) for k, v in (segments or {}).items() if (v.get("total_r") or 0) > 0}
    tot = sum(pos.values())
    return len(pos), (max(pos.values()) / tot if tot > 0 else None)


def evaluate(artifact: dict, policy: g.GatePolicy = g.GatePolicy()) -> dict:
    b: list[str] = []
    comp = {k: "BLOCK" for k in ("AI_EXPECTANCY_AUTHORITY", "AI_UPLIFT_AUTHORITY", "AI_CALIBRATION",
                                 "AI_COST_STRESS", "AI_PORTFOLIO_ROBUSTNESS", "AI_CONCENTRATION",
                                 "AI_WINDOWS")}
    cand = (artifact or {}).get("candidate_research") or {}
    ai = cand.get("ai_meta_model") or {}
    out = {"gate": GATE, "components": comp, "blockers": b, "verdict": "BLOCK",
           "recomputed": {}}
    if ai.get("status") != "OK":
        b.append(f"AI_META_MODEL_STATUS_{ai.get('status', 'MISSING')}")
        out["blockers"] = sorted(set(b))
        return _live_view(out, cand)
    if ai.get("data_label") != DATA_LABEL:
        b.append("AI_DATA_LABEL_NOT_ACKNOWLEDGED")
    if ai.get("population") != ai_hook.POPULATION or ai.get("hook_profile") != ai_hook.TRAINING_PROFILE:
        b.append("AI_POPULATION_NOT_RUNTIME_HOOK")

    # Required block length re-derived from the replay's resolved horizon.
    horizon = ((cand.get("inference") or {}).get("outcome_horizon_resolved_executable") or {})
    req = g.required_block_ms(horizon.get("max_ms"))
    if req is None:
        b.append("AI_OUTCOME_HORIZON_UNKNOWN")

    # Purged / embargoed window ordering: train < validation < test, gaps kept.
    ok_windows = True
    for s in ai.get("steps") or []:
        w = s.get("windows") or {}
        trn, va, te = w.get("train") or [], w.get("validation") or [], w.get("test") or []
        try:
            train_end = max(x[1] for x in trn)
            if not (train_end < va[0] and va[2] <= te[0] and va[1] < te[0]):
                ok_windows = False
        except (TypeError, ValueError, IndexError):
            ok_windows = False
    if not ai.get("steps") or not ok_windows:
        b.append("AI_WINDOWS_NOT_PURGED")
    else:
        comp["AI_WINDOWS"] = "PASS"

    auth = ai.get("authority") or {}
    appr = auth.get("ai_approved_expectancy") or {}
    upl = auth.get("ai_uplift_vs_baseline") or {}

    def samples_of(sec):
        bs = sec.get("bootstrap") or {}
        ok = (bs.get("model") == agg.VERSION and bs.get("seed") == inf.SEED
              and bs.get("samples") == tr.AUTHORITY_BOOTSTRAP_SAMPLES)
        return bs.get("samples") if ok else None
    re_e = agg.recompute(appr, required_ms=req, min_blocks=policy.min_resampling_blocks, paired=False,
                         samples=samples_of(appr))
    re_u = agg.recompute(upl, required_ms=req, min_blocks=policy.min_resampling_blocks, paired=True,
                         samples=samples_of(upl))
    lo, st, mean_r = re_e["ci"][0], re_e["status"], re_e["point_estimate"]
    lo_u, st_u, delta = re_u["ci"][0], re_u["status"], re_u["point_estimate"]
    out["recomputed"] = {"model": agg.VERSION, "ai_expectancy": re_e, "ai_uplift": re_u,
                         "required_block_ms": req}
    n_appr = (ai.get("pooled_test") or {}).get("ai_approved", {}).get("n") or 0
    if n_appr < policy.min_approved_samples:
        b.append("AI_INSUFFICIENT_APPROVED_SAMPLE")
    if st == "VALID" and lo is not None and lo > 0 and mean_r is not None and mean_r > 0:
        comp["AI_EXPECTANCY_AUTHORITY"] = "PASS"
    else:
        b.append(f"AI_EXPECTANCY_AUTHORITY_{st}" if st != "VALID" else "AI_EXPECTANCY_CI_NOT_POSITIVE")
    if st_u == "VALID" and lo_u is not None and lo_u > 0 and delta is not None and delta > 0:
        comp["AI_UPLIFT_AUTHORITY"] = "PASS"
    else:
        b.append(f"AI_UPLIFT_AUTHORITY_{st_u}" if st_u != "VALID" else "AI_UPLIFT_CI_NOT_POSITIVE")

    # Calibration: the calibrated probability must beat the base rate on EVERY test fold.
    cals = [s.get("test_calibration") or {} for s in ai.get("steps") or []]
    if cals and all(c.get("beats_base_rate") is True for c in cals):
        comp["AI_CALIBRATION"] = "PASS"
    else:
        b.append("AI_CALIBRATION_DOES_NOT_BEAT_BASE_RATE")

    pooled = ai.get("pooled_test") or {}
    n_sym, sym_share = _share(pooled.get("ai_by_symbol"))
    _, per_share = _share(pooled.get("ai_by_month"))
    conc_ok = True
    if n_sym < policy.min_symbols_contributing:
        b.append("AI_TOO_FEW_SYMBOLS_CONTRIBUTING")
        conc_ok = False
    if sym_share is None or sym_share > policy.max_symbol_share_of_positive_r:
        b.append("AI_SINGLE_SYMBOL_DOMINATES")
        conc_ok = False
    if per_share is None or per_share > policy.max_period_share_of_positive_r:
        b.append("AI_SINGLE_PERIOD_DOMINATES")
        conc_ok = False
    if conc_ok:
        comp["AI_CONCENTRATION"] = "PASS"

    stress = pooled.get("ai_cost_stress") or {}
    stress_ok = True
    for name in COST_SCENARIOS_REQUIRED:
        e = (stress.get(name) or {}).get("net_expectancy_r")
        if e is None or e <= 0:
            b.append(f"AI_COST_STRESS_FAILS_{name.upper()}")
            stress_ok = False
    if stress_ok:
        comp["AI_COST_STRESS"] = "PASS"

    folds = ai.get("portfolio_by_test_fold") or []
    port_ok = bool(folds)
    for f in folds:
        start, end = f.get("starting_equity"), f.get("ending_equity")
        realized, mdd, lim = f.get("realized_return"), f.get("portfolio_max_drawdown"), f.get(
            "research_max_drawdown_limit")
        if (start is None or end is None or end <= start or realized is None or realized <= 0
                or mdd is None or lim is None or mdd > lim
                or (f.get("end_state") or {}).get("portfolio_censoring_material") is not False):
            port_ok = False
    if port_ok:
        comp["AI_PORTFOLIO_ROBUSTNESS"] = "PASS"
    else:
        b.append("AI_PORTFOLIO_NOT_POSITIVE_ON_EVERY_TEST_FOLD" if folds else "AI_PORTFOLIO_MISSING")

    out["blockers"] = sorted(set(b))
    out["verdict"] = "PASS" if not b and all(v == "PASS" for v in comp.values()) else "BLOCK"
    return _live_view(out, cand)


def _live_view(out: dict, cand: dict) -> dict:
    """AI_HOOK_EDGE: selection quality among candidates reaching the runtime
    hook (research; may justify a zero-order SHADOW observation only).
    AI_EFFECTIVE_EXECUTION_EDGE needs every downstream execution filter
    represented, or fresh forward evidence. LIVE fails closed without it."""
    parity = (cand.get("ai_effective_execution_parity") or {}).get("status", "INCOMPLETE")
    out["ai_hook_edge"] = out["verdict"]
    out["ai_effective_execution_parity"] = parity
    live_b = list(out["blockers"])
    if parity != "COMPLETE":
        live_b.append("AI_EFFECTIVE_EXECUTION_PARITY_INCOMPLETE")
    out["ai_effective_execution_edge"] = "PASS" if (out["verdict"] == "PASS" and parity == "COMPLETE") else "BLOCK"
    out["live_historical_promotion"] = {"verdict": "PASS" if not live_b else "BLOCK",
                                        "blockers": sorted(set(live_b))}
    return out
