"""Offline AI meta-model training with PURGED walk-forward (never random splits).

Data labels (data-snooping control): the replay history has been inspected
repeatedly during Phases 2-7, so every window here is
``HISTORICAL_OOS_PREVIOUSLY_INSPECTED`` — never a pristine final test. Final
LIVE promotion additionally requires fresh forward SHADOW/PAPER evidence.

Layout: four purged/embargoed calendar folds F1..F4 (inf.purged_calendar_folds,
gaps >= the resolved outcome horizon):
    step 1: TRAIN F1        -> VALIDATION F2 -> TEST F3
    step 2: TRAIN F1+F2     -> VALIDATION F3 -> TEST F4
Model selection, hyperparameters, calibration, thresholds, enabled directions
and supported regimes are chosen on VALIDATION only, from small PREDECLARED
grids. Each TEST window is evaluated exactly once with the frozen result.

Targets: Model A = P(realized NET R > 0) (production-parity modeled_net_r);
Model B = E[gross_market_r]. Decision (training == runtime, decision.cost_contract):
    predicted_net_r = predicted_gross_r - (fees + CONSERVATIVE_SLIPPAGE_BUFFER)_r
                      - funding_r - decision_uncertainty_buffer_r
where the fee + slippage estimate is cost_fraction / stop_distance_pct.
"""
from __future__ import annotations

import hashlib
import json

import numpy as np

from bot import nexus_oos_inference as inf
from bot.ai import calibration as cal
from bot.ai import features as fx
from bot.ai import models as mdl
from bot.ai.decision import ABSTAIN_ALL, TRADABLE_REGIMES, DecisionPolicy, LONG, SHORT

DATA_LABEL = "HISTORICAL_OOS_PREVIOUSLY_INSPECTED"
# Predeclared, deliberately small candidate sets (no brute-force search).
CLASSIFIER_GRID = (("LOGISTIC_L2", {"l2": 1.0}), ("LOGISTIC_L2", {"l2": 10.0}),
                   ("BOOSTED_STUMPS", {"n_estimators": 60, "learning_rate": 0.1}),
                   ("NEXUS_HEURISTIC", {}))
REGRESSOR_GRID = (("RIDGE", {"l2": 10.0}), ("RIDGE", {"l2": 100.0}),
                  ("BOOSTED_STUMPS", {"n_estimators": 60, "learning_rate": 0.05, "loss": "squared"}))
P_GRID = (0.45, 0.50, 0.55, 0.60)
EDGE_GRID = (0.0, 0.05, 0.10)
MIN_VALIDATION_TRADES = 30
MIN_GROUP_TRADES = 30
ENABLED, DISABLED_NEGATIVE, DISABLED_INSUFFICIENT = (
    "ENABLED", "DISABLED_NEGATIVE", "DISABLED_INSUFFICIENT_EVIDENCE")

_CF = fx.MODEL_FEATURES.index("cost_fraction")
_SD = fx.MODEL_FEATURES.index("stop_distance_pct")


def cost_estimate_r(cost_fraction: float, stop_distance_pct: float) -> float:
    """Decision-time (fees + CONSERVATIVE_SLIPPAGE_BUFFER) in R units."""
    return float(cost_fraction) / float(stop_distance_pct) if stop_distance_pct and stop_distance_pct > 0 else 1e9


def dataset(rows):
    """AI_RUNTIME_HOOK_POPULATION_V1 rows only (``ai_hook_eligible``: the
    candidate reaches TradingEngine._ai_gate), RESOLVED, with canonical AI
    features of the current schema. NEXUS-rejected / geometry-blocked
    candidates never enter training, validation or test."""
    out = [r for r in rows if r.get("ai_hook_eligible") is True and r.get("outcome_status") == "RESOLVED"
           and r.get("r") is not None and isinstance(r.get("ai_features"), list)
           and len(r["ai_features"]) == len(fx.MODEL_FEATURES)]
    return sorted(out, key=lambda r: int(r["ts"]))


def _gross(row) -> float:
    g = row.get("gross_r")
    return float(g) if g is not None else float(row["r"])


def _xy(rows):
    X = np.array([r["ai_features"] for r in rows], float)
    X = np.nan_to_num(X, nan=0.0, posinf=0.0, neginf=0.0)
    r = np.array([float(x["r"]) for x in rows], float)
    return X, (r > 0).astype(float), r


def _gross_y(rows):
    return np.array([_gross(x) for x in rows], float)


def _make(kind, hp, role):
    if kind == "LOGISTIC_L2":
        return mdl.LogisticL2(**hp)
    if kind == "RIDGE":
        return mdl.Ridge(**hp)
    if kind == "BOOSTED_STUMPS":
        return mdl.BoostedStumps(**{"loss": "logistic" if role == "A" else "squared", **hp})
    if kind == "NEXUS_HEURISTIC":
        return mdl.NexusHeuristic(fx.MODEL_FEATURES.index("nexus_confidence"))
    raise ValueError(kind)


def net_r_estimate(row, predicted_gross_r: float, buffer_r: float) -> float:
    f = row["ai_features"]
    return predicted_gross_r - cost_estimate_r(f[_CF] or 0.0, f[_SD] or 0.0) - float(buffer_r)


def approve(rows, p, er, policy: DecisionPolicy):
    out = []
    for row, pp, ee in zip(rows, p, er):
        d = str(row.get("direction", "")).upper()
        reg = row.get("ai_regime", "UNKNOWN")
        if (d in policy.allowed_directions and reg in policy.supported_regimes
                and pp >= policy.min_p_profitable
                and net_r_estimate(row, ee, policy.uncertainty_buffer_r) > policy.min_expected_net_r):
            out.append(row)
    return out


def _mean(rows):
    return (sum(float(r["r"]) for r in rows) / len(rows)) if rows else None


def _status(rows):
    if len(rows) < MIN_GROUP_TRADES:
        return DISABLED_INSUFFICIENT
    return ENABLED if (_mean(rows) or 0.0) > 0 else DISABLED_NEGATIVE


def select_policy(val_rows, p_val, er_val, *, probability_authorizes: bool) -> tuple[DecisionPolicy, dict]:
    """Validation-only, FAIL-CLOSED selection.
    1. (p, edge) from the predeclared grid maximizing validation mean R with all
       directions and tradable regimes admitted (>= MIN_VALIDATION_TRADES).
    2. At THOSE FINAL thresholds, each direction and then each regime is
       ENABLED only with >= MIN_GROUP_TRADES and positive mean R. Insufficient
       evidence DISABLES. UNKNOWN / EXTREME are never tradable. No fallback.
    3. Nothing enabled => ABSTAIN_ALL."""
    table, best = [], None
    for pth in P_GRID:
        for edge in EDGE_GRID:
            pol = DecisionPolicy(pth, edge, allowed_directions=(LONG, SHORT),
                                 supported_regimes=TRADABLE_REGIMES)
            appr = approve(val_rows, p_val, er_val, pol)
            m = _mean(appr)
            table.append({"p": pth, "edge": edge, "n": len(appr), "mean_r": m})
            if len(appr) >= MIN_VALIDATION_TRADES and m is not None and (best is None or m > best[0]):
                best = (m, pth, edge)
    report = {"grid": table, "directions": {}, "regimes": {}}
    if best is None:
        report["policy"] = "ABSTAIN_ALL"
        return ABSTAIN_ALL, report
    _, pth, edge = best
    dirs = []
    for d in (LONG, SHORT):
        pol = DecisionPolicy(pth, edge, allowed_directions=(d,), supported_regimes=TRADABLE_REGIMES)
        appr = approve(val_rows, p_val, er_val, pol)
        st = _status(appr)
        report["directions"][d] = {"n": len(appr), "mean_r": _mean(appr), "status": st}
        if st == ENABLED:
            dirs.append(d)
    regs = []
    for reg in TRADABLE_REGIMES:
        pol = DecisionPolicy(pth, edge, allowed_directions=tuple(dirs), supported_regimes=(reg,))
        appr = approve(val_rows, p_val, er_val, pol) if dirs else []
        st = _status(appr)
        report["regimes"][reg] = {"n": len(appr), "mean_r": _mean(appr), "status": st}
        if st == ENABLED:
            regs.append(reg)
    for reg in ("UNKNOWN", "EXTREME"):
        report["regimes"][reg] = {"n": 0, "mean_r": None, "status": "DISABLED_NEVER_TRADABLE"}
    if not dirs or not regs:
        report["policy"] = "ABSTAIN_ALL"
        return ABSTAIN_ALL, report
    pol = DecisionPolicy(pth, edge, allowed_directions=tuple(dirs), supported_regimes=tuple(regs),
                         probability_authorizes=probability_authorizes)
    report["policy"] = "SELECTED"
    return pol, report


def _fit_select(train, val):
    Xt, yt, _ = _xy(train)
    Xv, yv, _ = _xy(val)
    rt, rv = _gross_y(train), _gross_y(val)
    cls_scores = []
    for kind, hp in CLASSIFIER_GRID:
        m = _make(kind, hp, "A").fit(Xt, yt)
        cls_scores.append((cal.brier(m.predict_proba(Xv), yv), kind, hp, m))
    cls_scores.sort(key=lambda t: t[0])
    _, kc, hpc, clf = cls_scores[0]
    reg_scores = []
    for kind, hp in REGRESSOR_GRID:
        m = _make(kind, hp, "B").fit(Xt, rt)
        reg_scores.append((float(np.mean((m.predict(Xv) - rv) ** 2)), kind, hp, m))
    reg_scores.sort(key=lambda t: t[0])
    _, kr, hpr, reg = reg_scores[0]
    calibrator = cal.Platt().fit(clf.predict_proba(Xv), yv)
    p_val = calibrator.transform(clf.predict_proba(Xv))
    er_val = reg.predict(Xv)
    val_cal = cal.report(p_val, yv)
    policy, sel = select_policy(val, list(p_val), list(er_val),
                                probability_authorizes=bool(val_cal["beats_base_rate"]))
    return {"classifier": clf, "classifier_kind": kc, "classifier_hp": hpc, "regressor": reg,
            "regressor_kind": kr, "regressor_hp": hpr, "calibrator": calibrator, "policy": policy,
            "selection": sel,
            "validation_brier": [{"kind": k, "hp": h, "brier": b} for b, k, h, _ in cls_scores],
            "validation_mse": [{"kind": k, "hp": h, "mse": s} for s, k, h, _ in reg_scores],
            "validation_calibration": val_cal}


def _segment(rows, key):
    out = {}
    for r in rows:
        out.setdefault(str(r.get(key)), []).append(float(r["r"]))
    return {k: {"n": len(v), "mean_r": sum(v) / len(v), "total_r": sum(v)} for k, v in sorted(out.items())}


def _key(r):
    return [int(r["ts"]), str(r.get("symbol")), str(r.get("direction"))]


def selection_stability(steps) -> dict:
    """Predeclared rule: STABLE iff every step selected the same classifier
    (kind + hyperparameters), the same regressor and the IDENTICAL decision
    policy (decision_policy_sha256: thresholds, directions, supported regimes,
    probability_authorizes, uncertainty buffer, freshness, latency, version),
    and no step is ABSTAIN_ALL."""
    sig = [(json.dumps(s["selected_classifier"], sort_keys=True),
            json.dumps(s["selected_regressor"], sort_keys=True),
            DecisionPolicy.from_json(s["policy"]).sha256) for s in steps]
    abstain = any(not s["policy"]["allowed_directions"] for s in steps)
    stable = bool(sig) and not abstain and all(x == sig[0] for x in sig)
    return {"status": "MODEL_SELECTION_STABLE" if stable else "MODEL_SELECTION_UNSTABLE",
            "rule": "same classifier, regressor and decision_policy_sha256 in every step; no ABSTAIN_ALL",
            "policy_sha256_by_step": [x[2] for x in sig]}


AUTHORITY_BOOTSTRAP_SAMPLES = 1000      # predeclared; the AI gate requires exactly this


def walk_forward(rows, *, required_ms: int, samples: int = AUTHORITY_BOOTSTRAP_SAMPLES) -> dict:
    from bot import nexus_oos_research as res
    data = dataset(rows)
    from bot.ai import hook as ai_hook
    base = {"population": ai_hook.POPULATION, "hook_profile": ai_hook.TRAINING_PROFILE,
            "data_label": DATA_LABEL, "feature_schema": fx.FEATURE_SCHEMA_VERSION,
            "feature_schema_sha256": fx.schema_hash(), "model_features": list(fx.MODEL_FEATURES),
            "classifier_grid": [list(x) for x in CLASSIFIER_GRID],
            "regressor_grid": [list(x) for x in REGRESSOR_GRID], "p_grid": list(P_GRID),
            "edge_grid": list(EDGE_GRID), "required_block_ms": int(required_ms),
            "min_group_trades": MIN_GROUP_TRADES}
    if len(data) < 200:
        return {**base, "status": "INSUFFICIENT_DATA", "rows": len(data)}
    lay = inf.purged_calendar_folds(int(data[0]["ts"]), int(data[-1]["ts"]) + 1,
                                    required_horizon_ms=required_ms)
    if lay["status"] != "OK":
        return {**base, "status": "INSUFFICIENT_INDEPENDENT_FOLDS", "layout": lay}
    W = lay["windows"]

    def inwin(w):
        return [r for r in data if w["decision_start_ts"] <= int(r["ts"]) < w["decision_end_ts"]
                and int(r["outcome_end_ts"]) < w["outcome_window_end_ts"]]
    folds = [inwin(w) for w in W]
    steps, approved_test, all_test, nexus_test, fitted = [], [], [], [], []
    for i, (tr_idx, va, te) in enumerate((([0], 1, 2), ([0, 1], 2, 3))):
        train = [r for k in tr_idx for r in folds[k]]
        val, test = folds[va], folds[te]
        fit = _fit_select(train, val)
        fitted.append(fit)
        Xs, ys, _ = _xy(test)
        p_test = fit["calibrator"].transform(fit["classifier"].predict_proba(Xs))
        er_test = fit["regressor"].predict(Xs)
        appr = approve(test, list(p_test), list(er_test), fit["policy"])
        approved_test += appr
        all_test += test
        nexus_test += [r for r in test if r.get("approved")]
        steps.append({
            "step": i + 1, "train_folds": [k + 1 for k in tr_idx], "validation_fold": va + 1,
            "test_fold": te + 1,
            "windows": {"train": [[W[k]["decision_start_ts"], W[k]["decision_end_ts"]] for k in tr_idx],
                        "validation": [W[va]["decision_start_ts"], W[va]["decision_end_ts"],
                                       W[va]["outcome_window_end_ts"]],
                        "test": [W[te]["decision_start_ts"], W[te]["decision_end_ts"],
                                 W[te]["outcome_window_end_ts"]]},
            "n_train": len(train), "n_validation": len(val), "n_test": len(test),
            "selected_classifier": [fit["classifier_kind"], fit["classifier_hp"]],
            "selected_regressor": [fit["regressor_kind"], fit["regressor_hp"]],
            "policy": fit["policy"].to_json(), "policy_sha256": fit["policy"].sha256,
            "selection": fit["selection"],
            "validation_brier": fit["validation_brier"], "validation_mse": fit["validation_mse"],
            "validation_calibration": fit["validation_calibration"],
            "test_calibration": cal.report(p_test, ys),
            "test_ai_approved": {"n": len(appr), "mean_r": _mean(appr)},
            "test_baseline": {"n": len(test), "mean_r": _mean(test)},
            "test_nexus_production": {"n": sum(1 for r in test if r.get("approved")),
                                      "mean_r": _mean([r for r in test if r.get("approved")])},
            "test_ai_by_direction": _segment(appr, "direction"),
            "test_ai_by_symbol": _segment(appr, "symbol"),
            "test_ai_by_regime": _segment(appr, "ai_regime"),
            "test_ai_cost_stress": res.cost_stress(appr),
            "_approved_keys": [_key(r) for r in appr],
        })
    ids = {id(r) for r in approved_test}
    pooled = sorted((dict(r, ai_approved=id(r) in ids) for r in all_test), key=lambda r: int(r["ts"]))
    authority = None
    if approved_test:
        authority = {
            "ai_approved_expectancy": inf.dependence_aware_mean(
                pooled, lambda r: r["ai_approved"], samples=samples, required_ms=required_ms),
            "ai_uplift_vs_baseline": inf.dependence_aware_diff(
                pooled, lambda r: r["ai_approved"], lambda r: True, samples=samples,
                required_ms=required_ms)}
        # Compact per-block aggregates are RETAINED at every authoritative block
        # length: the AI gate recomputes the bootstrap CIs, the residual ACF and
        # the authority interval (AI_AGGREGATE_BLOCK_BOOTSTRAP_V1).
        for sec in authority.values():
            sec["bootstrap"] = {"model": "AI_AGGREGATE_BLOCK_BOOTSTRAP_V1", "samples": int(samples),
                                "seed": inf.SEED, "unit_order": "BLOCK_ID_ASCENDING",
                                "percentiles": [0.025, 0.975]}
    return {**base, "status": "OK", "layout": {k: v for k, v in lay.items() if k != "windows"},
            "dataset_manifest": dataset_manifest(rows),
            "folds": [{"fold": w["fold"], "decision_start_ts": w["decision_start_ts"],
                       "decision_end_ts": w["decision_end_ts"],
                       "outcome_window_end_ts": w["outcome_window_end_ts"], "n": len(f)}
                      for w, f in zip(W, folds)],
            "steps": steps, "selection_stability": selection_stability(steps),
            "pooled_test": {"ai_approved": {"n": len(approved_test), "mean_r": _mean(approved_test)},
                            "baseline": {"n": len(all_test), "mean_r": _mean(all_test)},
                            "nexus_production": {"n": len(nexus_test), "mean_r": _mean(nexus_test)},
                            "ai_by_direction": _segment(approved_test, "direction"),
                            "ai_by_symbol": _segment(approved_test, "symbol"),
                            "ai_by_month": _segment(approved_test, "month"),
                            "ai_by_regime": _segment(approved_test, "ai_regime"),
                            "ai_cost_stress": res.cost_stress(approved_test)},
            "authority": authority,
            "_fitted": fitted,
            "interpretation": ("research evidence on previously inspected history; promotion "
                               "requires the AI gate AND fresh forward SHADOW/PAPER evidence")}


def dataset_manifest(rows) -> dict:
    """Identity of EXACTLY the rows training.dataset() passes to training
    (AI_RUNTIME_HOOK_POPULATION_V1, resolved, current schema). One row more or
    less, or any changed feature hash / outcome, changes the sha256."""
    from bot.ai import hook as ai_hook
    data = dataset(rows)
    body = [[int(r["ts"]), r.get("symbol"), r.get("direction"), r.get("ai_feature_hash"),
             round(float(r["r"]), 12)] for r in data]
    sha = hashlib.sha256(json.dumps(body).encode()).hexdigest()
    return {"rows": len(data), "sha256": sha,
            "training_dataset_rows": len(data), "training_dataset_sha256": sha,
            "training_population": ai_hook.POPULATION, "training_hook_profile": ai_hook.TRAINING_PROFILE,
            "first_ts": data[0]["ts"] if data else None, "last_ts": data[-1]["ts"] if data else None,
            "data_label": DATA_LABEL, "feature_schema_sha256": fx.schema_hash()}


def build_shadow_challenger(rows, wf: dict, *, training_code_sha: str | None, created_at: str,
                            lifecycle_state: str = "SHADOW_CHALLENGER") -> dict:
    """ONLY after the AI research gate PASSED and selection is STABLE (the
    caller enforces both). Refit the step-2 selections on all development
    folds F1..F3, calibrate on F4, keep the step-2 validated policy. This is
    NOT new OOS evidence; it is the model to be evaluated prospectively."""
    from bot.ai import decision as dec
    data = dataset(rows)
    folds = wf["folds"]
    cut = folds[3]["decision_start_ts"]
    dev = [r for r in data if int(r["ts"]) < folds[2]["decision_end_ts"]]
    calib = [r for r in data if int(r["ts"]) >= cut]
    step = wf["steps"][-1]
    (kc, hpc), (kr, hpr) = step["selected_classifier"], step["selected_regressor"]
    Xd, yd, _ = _xy(dev)
    clf = _make(kc, hpc, "A").fit(Xd, yd)
    reg = _make(kr, hpr, "B").fit(Xd, _gross_y(dev))
    Xc, yc, _ = _xy(calib)
    calibrator = cal.Platt().fit(clf.predict_proba(Xc), yc)
    policy = DecisionPolicy.from_json(step["policy"])
    manifest_ds = dataset_manifest(rows)
    common = dict(feature_names=fx.MODEL_FEATURES, feature_schema_sha256=fx.schema_hash(),
                  training_manifest=manifest_ds, training_code_sha=training_code_sha,
                  created_at=created_at,
                  training_period={"first_ts": dev[0]["ts"] if dev else None,
                                   "last_ts": dev[-1]["ts"] if dev else None})
    ca = mdl.artifact(clf, role="A", hyperparameters=hpc, calibration=calibrator.to_json(), **common)
    ra = mdl.artifact(reg, role="B", hyperparameters=hpr, **common)
    bundle = dec.bundle_manifest(
        classifier_artifact=ca, regressor_artifact=ra, calibration=calibrator.to_json(), policy=policy,
        training_code_sha=training_code_sha, dataset_manifest_sha256=manifest_ds["sha256"],
        training_period=common["training_period"],
        selection_evidence={"steps": [s["policy_sha256"] for s in wf["steps"]],
                            "stability": wf["selection_stability"]["status"]},
        created_at=created_at, lifecycle_state=lifecycle_state)
    return {"created": True, "lifecycle_state": lifecycle_state, "bundle": bundle, "classifier_artifact": ca,
            "regressor_artifact": ra, "bundle_sha256": bundle["bundle_sha256"],
            "decision_policy_sha256": policy.sha256, "feature_schema_sha256": fx.schema_hash(),
            "note": "not new OOS evidence; prospective SHADOW evaluation only; never LIVE_CHAMPION"}
