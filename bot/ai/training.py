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

Targets: Model A = P(realized NET R > 0) (production-parity R after fees,
slippage and funding); Model B = expected GROSS R. The decision rule charges
the same deterministic cost estimate in training and live:
    expected_net_r = E[gross R] - cost_fraction / stop_distance_pct - buffer
"""
from __future__ import annotations

import hashlib
import json

import numpy as np

from bot import nexus_oos_inference as inf
from bot.ai import calibration as cal
from bot.ai import features as fx
from bot.ai import models as mdl
from bot.ai.decision import DecisionPolicy, LONG, SHORT

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


def dataset(rows):
    """Executable, RESOLVED rows carrying canonical AI features."""
    out = [r for r in rows if r.get("executable") and r.get("outcome_status") == "RESOLVED"
           and r.get("r") is not None and isinstance(r.get("ai_features"), list)]
    return sorted(out, key=lambda r: int(r["ts"]))


_CF = fx.MODEL_FEATURES.index("cost_fraction")
_SD = fx.MODEL_FEATURES.index("stop_distance_pct")


def cost_estimate_r(cost_fraction: float, stop_distance_pct: float) -> float:
    """Round-trip execution cost in R units (deterministic, decision-time)."""
    return float(cost_fraction) / float(stop_distance_pct) if stop_distance_pct and stop_distance_pct > 0 else 1e9


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


def _approve(rows, p, er, policy: DecisionPolicy):
    out = []
    for row, pp, ee in zip(rows, p, er):
        d = str(row.get("direction", "")).upper()
        reg = row.get("ai_regime", "UNKNOWN")
        f = row["ai_features"]
        net = ee - cost_estimate_r(f[_CF] or 0.0, f[_SD] or 0.0) - float(policy.uncertainty_buffer_r)
        if (d in policy.allowed_directions and reg in policy.supported_regimes
                and pp >= policy.min_p_profitable and net > policy.min_expected_net_r):
            out.append(row)
    return out


def _mean(rows):
    return (sum(float(r["r"]) for r in rows) / len(rows)) if rows else None


def select_policy(val_rows, p_val, er_val) -> tuple[DecisionPolicy, dict]:
    """Validation-only threshold / direction / regime selection."""
    dirs = []
    for d in (LONG, SHORT):
        sub = [(r, a, b) for r, a, b in zip(val_rows, p_val, er_val) if str(r.get("direction")).upper() == d]
        base = DecisionPolicy(min(P_GRID), min(EDGE_GRID), allowed_directions=(d,))
        appr = _approve([s[0] for s in sub], [s[1] for s in sub], [s[2] for s in sub], base)
        if len(appr) >= MIN_GROUP_TRADES and (_mean(appr) or -1) > 0:
            dirs.append(d)
    best, table = None, []
    for pth in P_GRID:
        for edge in EDGE_GRID:
            pol = DecisionPolicy(pth, edge, allowed_directions=tuple(dirs) or (LONG,))
            appr = _approve(val_rows, p_val, er_val, pol) if dirs else []
            m = _mean(appr)
            table.append({"p": pth, "edge": edge, "n": len(appr), "mean_r": m})
            if len(appr) >= MIN_VALIDATION_TRADES and m is not None and (best is None or m > best[0]):
                best = (m, pol)
    if best is None:     # nothing survives validation => the AI abstains on everything
        return DecisionPolicy(1.01, 1e9, allowed_directions=()), {"grid": table, "enabled_directions": []}
    pol = best[1]
    regimes = []
    for reg in pol.supported_regimes:
        sub = [(r, a, b) for r, a, b in zip(val_rows, p_val, er_val) if r.get("ai_regime") == reg]
        appr = _approve([s[0] for s in sub], [s[1] for s in sub], [s[2] for s in sub], pol)
        if len(appr) < MIN_GROUP_TRADES or (_mean(appr) or 0) >= 0:
            regimes.append(reg)          # too few to judge: keep; clearly negative: drop
    pol = DecisionPolicy(pol.min_p_profitable, pol.min_expected_net_r,
                         allowed_directions=pol.allowed_directions, supported_regimes=tuple(regimes))
    return pol, {"grid": table, "enabled_directions": list(pol.allowed_directions),
                 "supported_regimes": regimes}


def _fit_select(train, val):
    Xt, yt, _ = _xy(train)
    Xv, yv, _ = _xy(val)
    rt, rv = _gross_y(train), _gross_y(val)
    cls_scores = []
    for kind, hp in CLASSIFIER_GRID:
        m = _make(kind, hp, "A").fit(Xt, yt)
        cls_scores.append((cal.brier(m.predict_proba(Xv), yv), kind, hp, m))
    cls_scores.sort(key=lambda t: t[0])
    b_c, kc, hpc, clf = cls_scores[0]
    reg_scores = []
    for kind, hp in REGRESSOR_GRID:
        m = _make(kind, hp, "B").fit(Xt, rt)
        reg_scores.append((float(np.mean((m.predict(Xv) - rv) ** 2)), kind, hp, m))
    reg_scores.sort(key=lambda t: t[0])
    _, kr, hpr, reg = reg_scores[0]
    calibrator = cal.Platt().fit(clf.predict_proba(Xv), yv)
    p_val = calibrator.transform(clf.predict_proba(Xv))
    er_val = reg.predict(Xv)
    policy, sel = select_policy(val, list(p_val), list(er_val))
    return {"classifier": clf, "classifier_kind": kc, "classifier_hp": hpc, "regressor": reg,
            "regressor_kind": kr, "regressor_hp": hpr, "calibrator": calibrator, "policy": policy,
            "selection": sel,
            "validation_brier": [{"kind": k, "hp": h, "brier": b} for b, k, h, _ in cls_scores],
            "validation_mse": [{"kind": k, "hp": h, "mse": s} for s, k, h, _ in reg_scores],
            "validation_calibration": cal.report(p_val, yv)}


def _segment(rows, key):
    out = {}
    for r in rows:
        out.setdefault(str(r.get(key)), []).append(float(r["r"]))
    return {k: {"n": len(v), "mean_r": sum(v) / len(v)} for k, v in sorted(out.items())}


def walk_forward(rows, *, required_ms: int, samples: int = 1000) -> dict:
    data = dataset(rows)
    base = {"data_label": DATA_LABEL, "feature_schema": fx.FEATURE_SCHEMA_VERSION,
            "feature_schema_sha256": fx.schema_hash(), "model_features": list(fx.MODEL_FEATURES),
            "classifier_grid": [list(x) for x in CLASSIFIER_GRID],
            "regressor_grid": [list(x) for x in REGRESSOR_GRID], "p_grid": list(P_GRID),
            "edge_grid": list(EDGE_GRID)}
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
    steps, approved_test, all_test, nexus_test = [], [], [], []
    for i, (tr_idx, va, te) in enumerate((([0], 1, 2), ([0, 1], 2, 3))):
        train = [r for k in tr_idx for r in folds[k]]
        val, test = folds[va], folds[te]
        fit = _fit_select(train, val)
        Xs, ys, _ = _xy(test)
        p_test = fit["calibrator"].transform(fit["classifier"].predict_proba(Xs))
        er_test = fit["regressor"].predict(Xs)
        appr = _approve(test, list(p_test), list(er_test), fit["policy"])
        approved_test += appr
        all_test += test
        nexus_test += [r for r in test if r.get("approved")]
        steps.append({
            "step": i + 1, "train_folds": [k + 1 for k in tr_idx], "validation_fold": va + 1,
            "test_fold": te + 1,
            "windows": {"train": [W[k]["decision_start_ts"] for k in tr_idx],
                        "validation": [W[va]["decision_start_ts"], W[va]["decision_end_ts"]],
                        "test": [W[te]["decision_start_ts"], W[te]["decision_end_ts"]]},
            "n_train": len(train), "n_validation": len(val), "n_test": len(test),
            "selected_classifier": [fit["classifier_kind"], fit["classifier_hp"]],
            "selected_regressor": [fit["regressor_kind"], fit["regressor_hp"]],
            "policy": fit["policy"].to_json(), "selection": fit["selection"],
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
        })
    ids = {id(r) for r in approved_test}
    pooled = [dict(r, ai_approved=id(r) in ids) for r in all_test]
    authority = None
    if approved_test:
        authority = {
            "ai_approved_expectancy": inf.dependence_aware_mean(
                pooled, lambda r: r["ai_approved"], samples=samples, required_ms=required_ms),
            "ai_uplift_vs_baseline": inf.dependence_aware_diff(
                pooled, lambda r: r["ai_approved"], lambda r: True, samples=samples,
                required_ms=required_ms)}
        for sec in authority.values():
            for iv in sec.get("block_intervals", []):
                if iv.get("residual_dependence"):
                    iv["residual_dependence"].pop("series", None)       # keep the report compact
            if isinstance(sec.get("residual_dependence_at_longest_usable"), dict):
                sec["residual_dependence_at_longest_usable"].pop("series", None)
    return {**base, "status": "OK", "layout": {k: v for k, v in lay.items() if k != "windows"},
            "folds": [{"fold": w["fold"], "decision_start_ts": w["decision_start_ts"],
                       "decision_end_ts": w["decision_end_ts"], "n": len(f)} for w, f in zip(W, folds)],
            "steps": steps,
            "pooled_test": {"ai_approved": {"n": len(approved_test), "mean_r": _mean(approved_test)},
                            "baseline": {"n": len(all_test), "mean_r": _mean(all_test)},
                            "nexus_production": {"n": len(nexus_test), "mean_r": _mean(nexus_test)},
                            "ai_by_direction": _segment(approved_test, "direction"),
                            "ai_by_symbol": _segment(approved_test, "symbol")},
            "authority": authority,
            "interpretation": ("research evidence on previously inspected history; promotion "
                               "requires the formal gate AND fresh forward SHADOW/PAPER evidence")}


def dataset_manifest(rows) -> dict:
    data = dataset(rows)
    body = [[int(r["ts"]), r.get("symbol"), r.get("direction"), round(float(r["r"]), 12)] for r in data]
    return {"rows": len(data), "sha256": hashlib.sha256(json.dumps(body).encode()).hexdigest(),
            "first_ts": data[0]["ts"] if data else None, "last_ts": data[-1]["ts"] if data else None,
            "data_label": DATA_LABEL}
