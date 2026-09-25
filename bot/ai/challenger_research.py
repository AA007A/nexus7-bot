"""Phase 8C: predeclared, time-boxed, nested walk-forward challenger research.

Everything below is fixed BEFORE the search runs (SEARCH_SPEC is hashed into
the artifact). Data: the AI_RUNTIME_HOOK_POPULATION_V1 rows of one replay of
PRE-WINDOW public history (training.dataset()). All results are labelled
PREVIOUSLY_INSPECTED_RESEARCH_SUPPORT - never UNTOUCHED_OOS.

Design (nested, chronological):
  purged calendar folds F1..F4 (same layout as training.walk_forward)
  outer step 1: inner-train F1,    inner-validate F2, outer-evaluate F3
  outer step 2: inner-train F1+F2, inner-validate F3, outer-evaluate F4
  Every candidate is fitted on inner-train; thresholds, directions and regimes
  are chosen on inner-validate with the UNCHANGED fail-closed rules
  (MIN_VALIDATION_TRADES, MIN_GROUP_TRADES, positive validation mean R). The
  candidate with the best inner-validation score is chosen per step. Outer
  folds never select anything; outer results of NON-selected candidates are
  recorded in the ledger for transparency only (post-hoc, never used).

The runtime bundle shape is unchanged (one classifier -> calibrated
probability, one GROSS-R regressor, runtime subtracts decision-time costs,
DecisionPolicy thresholds/directions/regimes), so a frozen challenger runs on
the existing observer without architecture changes.
"""
from __future__ import annotations

import hashlib
import itertools
import json

import numpy as np

from bot import nexus_oos_inference as inf
from bot.ai import calibration as cal
from bot.ai import features as fx
from bot.ai import training as tr
from bot.ai.decision import ABSTAIN_ALL, TRADABLE_REGIMES, DecisionPolicy, LONG, SHORT

EVIDENCE_LABEL = "PREVIOUSLY_INSPECTED_RESEARCH_SUPPORT"
RESEARCH_VERSION = "AI_CHALLENGER_RESEARCH_V1"

# ── predeclared search space (32 model specs x 2 selection criteria = 64) ────
LABEL_MARGINS_R = (0.0, 0.25)                         # classifier label: net R > margin
CLASSIFIERS = (("LOGISTIC_L2", {"l2": 10.0}), ("BOOSTED_STUMPS", {"n_estimators": 60, "learning_rate": 0.1}))
SAMPLE_WEIGHTS = ("NONE", "ABS_NET_R")                # net-R ranking: weight by |net R|
REGRESSORS = (("RIDGE", {"l2": 100.0}),
              ("BOOSTED_STUMPS", {"n_estimators": 60, "learning_rate": 0.05, "loss": "squared"}))
CALIBRATIONS = ("PLATT", "IDENTITY")
SELECTION_CRITERIA = ("MAX_MEAN_R", "MAX_LOWER_1SE")  # robust: mean - 1 standard error

# ── predeclared supporting conditions (pooled outer evaluation, F3+F4) ───────
MIN_POOLED_TRADES = 60
MAX_TOP_SYMBOL_SHARE = 0.5
MAX_TOP_MONTH_SHARE = 0.5
MAX_BRIER_EXCESS_OVER_BASE = 0.01
MAX_ECE = 0.10
REQUIRED_COST_SCENARIOS = ("fees_plus_50pct", "slippage_x2", "combined_adverse")

SEARCH_SPEC = {
    "version": RESEARCH_VERSION, "evidence_label": EVIDENCE_LABEL,
    "label_margins_r": list(LABEL_MARGINS_R), "classifiers": [list(c) for c in CLASSIFIERS],
    "sample_weights": list(SAMPLE_WEIGHTS), "regressors": [list(r) for r in REGRESSORS],
    "calibrations": list(CALIBRATIONS), "selection_criteria": list(SELECTION_CRITERIA),
    "threshold_grid": {"p": list(tr.P_GRID), "edge": list(tr.EDGE_GRID)},
    "fail_closed_rules": {"min_validation_trades": tr.MIN_VALIDATION_TRADES,
                          "min_group_trades": tr.MIN_GROUP_TRADES,
                          "direction_regime_requires_positive_validation_mean_r": True,
                          "never_tradable_regimes": ["UNKNOWN", "EXTREME"]},
    "outer_steps": [{"train": [1], "validate": 2, "evaluate": 3}, {"train": [1, 2], "validate": 3, "evaluate": 4}],
    "supporting_conditions": {
        "stability": "the step-2 selected spec+criterion is also non-ABSTAIN with positive inner-validation "
                     "score in step 1, and both steps' selected policies share >=1 direction and >=1 regime",
        "non_abstain_both_steps": True,
        "min_pooled_trades": MIN_POOLED_TRADES,
        "expectancy_authority_valid_and_ci_low_gt_0": True,
        "uplift_vs_hook_baseline_authority_valid_and_ci_low_gt_0": True,
        "cost_stress_positive": list(REQUIRED_COST_SCENARIOS),
        "max_top_symbol_share_of_positive_r": MAX_TOP_SYMBOL_SHARE,
        "max_top_month_share_of_positive_r": MAX_TOP_MONTH_SHARE,
        "max_brier_excess_over_base_rate": MAX_BRIER_EXCESS_OVER_BASE, "max_ece": MAX_ECE},
    "freeze_rule": "refit selected spec on F1..F3, calibrate on F4, policy = step-2 validated policy",
}
SEARCH_SPEC_SHA256 = hashlib.sha256(json.dumps(SEARCH_SPEC, sort_keys=True).encode()).hexdigest()


def candidate_specs():
    for m, (ck, chp), w, (rk, rhp), c in itertools.product(LABEL_MARGINS_R, CLASSIFIERS, SAMPLE_WEIGHTS,
                                                           REGRESSORS, CALIBRATIONS):
        yield {"label_margin_r": m, "classifier": [ck, chp], "sample_weight": w, "regressor": [rk, rhp],
               "calibration": c}


def spec_id(spec) -> str:
    return hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()[:12]


def _labels(rows, margin):
    return np.array([1.0 if float(r["r"]) > margin else 0.0 for r in rows])


def _weights(rows, kind):
    if kind == "NONE":
        return None
    w = np.array([abs(float(r["r"])) for r in rows])
    return w / w.mean() if w.mean() > 0 else None


def fit_spec(spec, rows):
    X, _, _ = tr._xy(rows)
    y = _labels(rows, spec["label_margin_r"])
    clf = tr._make(spec["classifier"][0], spec["classifier"][1], "A")
    clf.fit(X, y, sample_weight=_weights(rows, spec["sample_weight"]))
    reg = tr._make(spec["regressor"][0], spec["regressor"][1], "B").fit(X, tr._gross_y(rows))
    return clf, reg


def predict(clf, reg, calibrator, rows):
    X, _, _ = tr._xy(rows)
    p = clf.predict_proba(X)
    return (calibrator.transform(p) if calibrator is not None else p), reg.predict(X)


def _score(appr, criterion):
    rs = [float(r["r"]) for r in appr]
    if len(rs) < tr.MIN_VALIDATION_TRADES:
        return None
    m = float(np.mean(rs))
    if criterion == "MAX_MEAN_R":
        return m
    return m - float(np.std(rs, ddof=1)) / np.sqrt(len(rs))


def select_policy(val, p_val, er_val, criterion, *, probability_authorizes):
    """training.select_policy with the grid cell chosen by ``criterion``;
    direction/regime gating is IDENTICAL (fail-closed, >=30, positive mean)."""
    best = None
    for pth in tr.P_GRID:
        for edge in tr.EDGE_GRID:
            pol = DecisionPolicy(pth, edge, allowed_directions=(LONG, SHORT), supported_regimes=TRADABLE_REGIMES)
            s = _score(tr.approve(val, p_val, er_val, pol), criterion)
            if s is not None and (best is None or s > best[0]):
                best = (s, pth, edge)
    if best is None:
        return ABSTAIN_ALL, None
    _, pth, edge = best
    dirs = [d for d in (LONG, SHORT) if tr._status(tr.approve(
        val, p_val, er_val, DecisionPolicy(pth, edge, allowed_directions=(d,),
                                           supported_regimes=TRADABLE_REGIMES))) == tr.ENABLED]
    regs = [g for g in TRADABLE_REGIMES if dirs and tr._status(tr.approve(
        val, p_val, er_val, DecisionPolicy(pth, edge, allowed_directions=tuple(dirs),
                                           supported_regimes=(g,)))) == tr.ENABLED]
    if not dirs or not regs:
        return ABSTAIN_ALL, None
    pol = DecisionPolicy(pth, edge, allowed_directions=tuple(dirs), supported_regimes=tuple(regs),
                         probability_authorizes=probability_authorizes)
    return pol, _score(tr.approve(val, p_val, er_val, pol), criterion)


def _calibrator(spec, clf, rows):
    if spec["calibration"] == "IDENTITY":
        return cal.Identity()
    X, _, _ = tr._xy(rows)
    return cal.Platt().fit(clf.predict_proba(X), _labels(rows, spec["label_margin_r"]))


def _mean(rows):
    return float(np.mean([float(r["r"]) for r in rows])) if rows else None


def run(rows, *, required_ms: int) -> dict:
    data = tr.dataset(rows)
    out = {"research_version": RESEARCH_VERSION, "evidence_label": EVIDENCE_LABEL,
           "search_spec": SEARCH_SPEC, "search_spec_sha256": SEARCH_SPEC_SHA256,
           "dataset_manifest": tr.dataset_manifest(rows), "rows": len(data)}
    if len(data) < 200:
        return {**out, "status": "INSUFFICIENT_DATA", "result": "NO_VALID_CHALLENGER"}
    lay = inf.purged_calendar_folds(int(data[0]["ts"]), int(data[-1]["ts"]) + 1, required_horizon_ms=required_ms)
    if lay["status"] != "OK":
        return {**out, "status": "INSUFFICIENT_INDEPENDENT_FOLDS", "result": "NO_VALID_CHALLENGER"}
    W = lay["windows"]
    folds = [[r for r in data if w["decision_start_ts"] <= int(r["ts"]) < w["decision_end_ts"]
              and int(r["outcome_end_ts"]) < w["outcome_window_end_ts"]] for w in W]
    specs = list(candidate_specs())
    ledger, steps = [], []
    for si, (tr_idx, va, ev) in enumerate((([0], 1, 2), ([0, 1], 2, 3))):
        train = [r for k in tr_idx for r in folds[k]]
        val, test = folds[va], folds[ev]
        best = None
        for spec in specs:
            clf, reg = fit_spec(spec, train)
            calib = _calibrator(spec, clf, val)
            p_val, er_val = predict(clf, reg, calib, val)
            vrep = cal.report(p_val, _labels(val, spec["label_margin_r"]))
            p_te, er_te = predict(clf, reg, calib, test)
            for crit in SELECTION_CRITERIA:
                pol, vscore = select_policy(val, list(p_val), list(er_val), crit,
                                            probability_authorizes=bool(vrep["beats_base_rate"]))
                appr_val = tr.approve(val, list(p_val), list(er_val), pol)
                appr_te = tr.approve(test, list(p_te), list(er_te), pol)
                entry = {"step": si + 1, "spec_id": spec_id(spec), "spec": spec, "criterion": crit,
                         "policy": pol.to_json(), "policy_sha256": pol.sha256,
                         "abstain_all": pol.sha256 == ABSTAIN_ALL.sha256,
                         "validation": {"n": len(appr_val), "mean_r": _mean(appr_val), "score": vscore,
                                        "brier": vrep["brier"], "brier_base": vrep["brier_base_rate"]},
                         "outer_posthoc_not_used_for_selection": {"n": len(appr_te), "mean_r": _mean(appr_te)}}
                ledger.append(entry)
                if vscore is not None and (best is None or vscore > best[0]):
                    best = (vscore, spec, crit, pol, clf, reg, calib, appr_te, p_te)
        if best is None:
            steps.append({"step": si + 1, "selected": None, "reason": "ALL_CANDIDATES_ABSTAIN_ON_VALIDATION",
                          "test": test, "approved": []})
            continue
        vscore, spec, crit, pol, clf, reg, calib, appr_te, p_te = best
        steps.append({"step": si + 1, "selected": {"spec_id": spec_id(spec), "spec": spec, "criterion": crit,
                                                   "policy": pol.to_json(), "policy_sha256": pol.sha256,
                                                   "validation_score": vscore},
                      "test": test, "approved": appr_te,
                      "test_calibration": cal.report(p_te, _labels(test, spec["label_margin_r"]))})
    out["ledger"] = ledger
    out["candidates_evaluated"] = len(specs) * len(SELECTION_CRITERIA)
    out["max_outer_posthoc_mean_r"] = max((e["outer_posthoc_not_used_for_selection"]["mean_r"]
                                          for e in ledger if e["outer_posthoc_not_used_for_selection"]["n"]
                                          >= tr.MIN_GROUP_TRADES), default=None)
    out["max_outer_posthoc_note"] = ("best post-hoc outer result across ALL candidates: shows the optimism of "
                                     "picking by outcome; NOT a result of the predeclared method")
    out["steps"] = [{k: v for k, v in s.items() if k not in ("test", "approved")}
                    | {"outer_eval": {"n_candidates": len(s["test"]), "hook_baseline_mean_r": _mean(s["test"]),
                                      "approved_n": len(s["approved"]), "approved_mean_r": _mean(s["approved"])}}
                    for s in steps]
    out["support"] = supporting_conditions(steps, required_ms, ledger)
    out["result"] = "CANDIDATE_SUPPORTED" if out["support"]["all_pass"] else "NO_VALID_CHALLENGER"
    out["_selected_spec"] = steps[-1]["selected"]
    out["_folds"] = folds
    return {**out, "status": "OK"}


def stability(sel, ledger) -> dict:
    """Predeclared: the final (step-2) spec+criterion must also be enabled
    with a positive inner-validation score in step 1, and the selected
    policies of both steps must overlap in direction and regime."""
    if not all(sel):
        return {"stable": False, "reason": "NO_SELECTION_IN_SOME_STEP"}
    fin = sel[-1]
    back = [e for e in ledger if e["step"] == 1 and e["spec_id"] == fin["spec_id"]
            and e["criterion"] == fin["criterion"]]
    back_ok = bool(back) and not back[0]["abstain_all"] and (back[0]["validation"]["score"] or 0) > 0
    p1, p2 = sel[0]["policy"], fin["policy"]
    overlap = bool(set(p1["allowed_directions"]) & set(p2["allowed_directions"])) and bool(
        set(p1["supported_regimes"]) & set(p2["supported_regimes"]))
    return {"stable": back_ok and overlap, "final_spec_enabled_in_step1": back_ok, "policy_overlap": overlap,
            "same_spec_both_steps": sel[0]["spec_id"] == fin["spec_id"]}


def supporting_conditions(steps, required_ms, ledger) -> dict:
    from bot import nexus_oos_research as res
    sel = [s["selected"] for s in steps]
    fails = []
    stab = stability(sel, ledger)
    if not stab["stable"]:
        fails.append("SELECTION_UNSTABLE_OR_MISSING")
    if not all(s and s["policy_sha256"] != ABSTAIN_ALL.sha256 for s in sel):
        fails.append("ABSTAIN_IN_SOME_STEP")
    test = sorted([r for s in steps for r in s["test"]], key=lambda r: int(r["ts"]))
    ids = {id(r) for s in steps for r in s["approved"]}
    appr = [r for r in test if id(r) in ids]
    rep = {"stability": stab, "pooled_hook_baseline": {"n": len(test), "mean_r": _mean(test)},
           "pooled_approved": {"n": len(appr), "mean_r": _mean(appr)}}
    if len(appr) < MIN_POOLED_TRADES:
        fails.append("TOO_FEW_POOLED_TRADES")
    if appr:
        pooled = [dict(r, _a=id(r) in ids) for r in test]
        exp = inf.dependence_aware_mean(pooled, lambda r: r["_a"], required_ms=required_ms)
        up = inf.dependence_aware_diff(pooled, lambda r: r["_a"], lambda r: True, required_ms=required_ms)
        rep["expectancy"] = {k: exp.get(k) for k in ("mean_r", "n", "authority_status", "authority_ci_low",
                                                     "authority_ci_high", "iid_ci")}
        rep["uplift"] = {k: up.get(k) for k in ("delta", "authority_status", "authority_ci_low",
                                                "authority_ci_high", "iid_ci")}
        if not (exp.get("authority_status") == inf.AUTHORITY_VALID and (exp.get("authority_ci_low") or -1) > 0):
            fails.append("EXPECTANCY_CI_NOT_POSITIVE")
        if not (up.get("authority_status") == inf.AUTHORITY_VALID and (up.get("authority_ci_low") or -1) > 0):
            fails.append("UPLIFT_CI_NOT_POSITIVE")
        stress = res.cost_stress(appr)
        rep["cost_stress"] = stress
        for sc in REQUIRED_COST_SCENARIOS:
            if not ((stress.get(sc) or {}).get("net_expectancy_r") or -1) > 0:
                fails.append(f"COST_STRESS_FAILS_{sc.upper()}")
        conc = {k: res.concentration(appr, k) for k in ("symbol", "month", "direction", "ai_regime")}
        rep["concentration"] = conc
        if (conc["symbol"].get("top_share_of_positive_r") or 1.0) > MAX_TOP_SYMBOL_SHARE:
            fails.append("SINGLE_SYMBOL_DOMINATES")
        if (conc["month"].get("top_share_of_positive_r") or 1.0) > MAX_TOP_MONTH_SHARE:
            fails.append("SINGLE_MONTH_DOMINATES")
        cals = [s.get("test_calibration") for s in steps if s.get("test_calibration")]
        rep["calibration"] = cals
        if any(c["brier"] - c["brier_base_rate"] > MAX_BRIER_EXCESS_OVER_BASE or (c["ece"] or 1) > MAX_ECE
               for c in cals):
            fails.append("CALIBRATION_FAILURE")
    else:
        fails.append("NO_APPROVED_TRADES")
    return {**rep, "failures": sorted(set(fails)), "all_pass": not fails, "label": EVIDENCE_LABEL}


def freeze(rows, research: dict, *, training_code_sha: str, created_at: str) -> dict:
    """Refit the selected spec on F1..F3, calibrate on F4, keep the step-2
    validated policy. Only when every supporting condition passed."""
    from bot.ai import decision as dec
    from bot.ai import models as mdl
    if research.get("result") != "CANDIDATE_SUPPORTED":
        raise ValueError("no supported candidate; refusing to freeze")
    sel = research["_selected_spec"]
    spec = sel["spec"]
    folds = research["_folds"]
    dev = folds[0] + folds[1] + folds[2]
    calib_rows = folds[3]
    clf, reg = fit_spec(spec, dev)
    calibrator = _calibrator(spec, clf, calib_rows)
    policy = DecisionPolicy.from_json(sel["policy"])
    ds = tr.dataset_manifest(rows)
    common = dict(feature_names=fx.MODEL_FEATURES, feature_schema_sha256=fx.schema_hash(), training_manifest=ds,
                  training_code_sha=training_code_sha, created_at=created_at,
                  training_period={"first_ts": dev[0]["ts"], "last_ts": dev[-1]["ts"],
                                   "calibration_last_ts": calib_rows[-1]["ts"] if calib_rows else None})
    ca = mdl.artifact(clf, role="A", hyperparameters={**spec["classifier"][1], "label_margin_r": spec["label_margin_r"],
                                                      "sample_weight": spec["sample_weight"]},
                      calibration=calibrator.to_json(), **common)
    ra = mdl.artifact(reg, role="B", hyperparameters=spec["regressor"][1], **common)
    bundle = dec.bundle_manifest(
        classifier_artifact=ca, regressor_artifact=ra, calibration=calibrator.to_json(), policy=policy,
        training_code_sha=training_code_sha, dataset_manifest_sha256=ds["sha256"],
        training_period=common["training_period"],
        selection_evidence={"research_version": RESEARCH_VERSION, "search_spec_sha256": SEARCH_SPEC_SHA256,
                            "spec_id": sel["spec_id"], "criterion": sel["criterion"],
                            "evidence_label": EVIDENCE_LABEL},
        created_at=created_at, lifecycle_state="SHADOW_CHALLENGER")
    return {"created": True, "lifecycle_state": "SHADOW_CHALLENGER", "bundle": bundle, "classifier_artifact": ca,
            "regressor_artifact": ra, "bundle_sha256": bundle["bundle_sha256"],
            "decision_policy_sha256": policy.sha256, "feature_schema_sha256": fx.schema_hash(),
            "claims": {"edge_claim": False, "order_authority": False, "live_authority": False},
            "note": EVIDENCE_LABEL + "; prospective SHADOW evaluation only"}


def public(research: dict) -> dict:
    return {k: v for k, v in research.items() if not k.startswith("_")}


def _estimate_per_72h(research: dict) -> dict:
    folds = research["_folds"]
    days = sum(((int(f[-1]["ts"]) - int(f[0]["ts"])) / 86_400_000.0) for f in folds[2:] if f)
    n = research["support"]["pooled_approved"]["n"]
    return {"outer_eval_days": days, "approved": n, "estimated_trades_per_72h": (n / days * 3.0) if days else None}


def main(argv=None) -> int:
    import argparse
    import gzip
    from pathlib import Path
    from bot import nexus_oos_replay_manifest as rm
    from bot.ai import bundle_export as bx
    ap = argparse.ArgumentParser(description="Phase 8C predeclared challenger research")
    ap.add_argument("--rows", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--candidate-sha", required=True)
    ap.add_argument("--created-at", required=True)
    a = ap.parse_args(argv)
    with gzip.open(a.rows, "rt", encoding="utf-8") as fh:
        header = json.loads(fh.readline())["header"]
        rows = [json.loads(line) for line in fh if line.strip()]
    research = run(rows, required_ms=int(header["required_ms"]))
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    pub = public(research)
    pub["replay_header"] = header
    pub["replay_policy_manifest_sha256"] = rm.load().sha256
    pub["candidate_code_sha"] = a.candidate_sha
    if research.get("status") == "OK":
        pub["estimate"] = _estimate_per_72h(research)
    summary = {k: pub.get(k) for k in ("research_version", "evidence_label", "search_spec_sha256", "rows",
                                       "status", "result", "candidates_evaluated", "max_outer_posthoc_mean_r",
                                       "steps", "support", "estimate", "dataset_manifest",
                                       "replay_policy_manifest_sha256", "candidate_code_sha")}
    if research.get("result") == "CANDIDATE_SUPPORTED":
        ch = freeze(rows, research, training_code_sha=a.candidate_sha, created_at=a.created_at)
        art = {"candidate_sha": a.candidate_sha, "candidate_research": {"ai_meta_model": {
            "shadow_challenger": ch, "dataset_manifest": tr.dataset_manifest(rows)}}}
        exp = bx.export(art, out / "challenger_bundle", candidate_sha=a.candidate_sha)
        ver = bx.verify(out / "challenger_bundle")
        summary["frozen"] = {"bundle_sha256": ch["bundle_sha256"], "policy": ch["bundle"]["decision_policy"],
                             "decision_policy_sha256": ch["decision_policy_sha256"],
                             "feature_schema_sha256": ch["feature_schema_sha256"],
                             "file_sha256": exp["file_sha256"], "verify": ver}
    (out / "challenger_research.json").write_text(json.dumps(pub, indent=1, sort_keys=True, default=str) + "\n")
    (out / "challenger_summary.json").write_text(json.dumps(summary, indent=1, sort_keys=True, default=str) + "\n")
    print(json.dumps(summary, indent=1, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
