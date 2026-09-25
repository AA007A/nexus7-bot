"""Phase 8D: SIGNAL_DISCOVERY_SPEC_V1 - cost-aware edge discovery (research only).

Everything is PREDECLARED in SPEC (hashed into every artifact) before the
search runs. Inputs: the AI_RUNTIME_HOOK_POPULATION_V1 rows of ONE replay of
pre-forward-window public history (dumped by the replay, research branch).

Stages
  A  signal diagnostics (segment tables)            - diagnostic only
  B  loss attribution (SIGNAL/ENTRY/EXIT/RISK/COST)   - diagnostic only
  C  horizon analysis on the forward 4h path          - diagnostic only
  E  feature diagnostics (+ research-only features)   - diagnostic only
  F  conditional-vs-global model comparison           - research only
  I  exit research (paired, identical population)     - research only
  D/G/H/K/L/M/N  nested promotable challenger search  - may freeze ONLY if
     every predeclared supporting gate passes.

Promotable candidates use ONLY the runtime feature schema (bot.ai.features)
and the unchanged runtime decision function (calibrated probability gate +
predicted GROSS R minus decision-time costs minus buffer). Research-only
features / targets / filters are reported separately and never frozen.
All evidence: PREVIOUSLY_INSPECTED_RESEARCH_SUPPORT (never UNTOUCHED_OOS).
"""
from __future__ import annotations

import gzip
import hashlib
import itertools
import json
import math
from collections import defaultdict

import numpy as np

from bot import nexus_oos_inference as inf
from bot.ai import calibration as cal
from bot.ai import features as fx
from bot.ai import research_features as rfx
from bot.ai import training as tr
from bot.ai.decision import ABSTAIN_ALL, TRADABLE_REGIMES, DecisionPolicy, LONG, SHORT

EVIDENCE_LABEL = "PREVIOUSLY_INSPECTED_RESEARCH_SUPPORT"
VERSION = "SIGNAL_DISCOVERY_SPEC_V1"
# No forward Phase-8 evidence may enter research: the Phase-8 observer never
# opened a window before this spec; rows at/after this cutoff are refused.
FORWARD_EVIDENCE_CUTOFF_MS = 1_790_000_000_000          # 2026-09-21 13:46:40 UTC (window never opened)
BAR_MS = 15 * 60_000
HORIZON_BARS = (1, 2, 4, 8, 16)                         # 15m, 30m, 60m, 120m, 240m
MAJORS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")

# promotable search space (runtime-exact semantics)
CLASS_TARGETS = ("NET_GT_0", "NET_GT_025", "MFE1_BEFORE_MAE1", "HIGH_QUALITY_NET_GT_05")
CLASSIFIERS = (("LOGISTIC_L2", {"l2": 1.0}), ("LOGISTIC_L2", {"l2": 10.0}),
               ("BOOSTED_STUMPS", {"n_estimators": 60, "learning_rate": 0.1}))
REGRESSORS = (("RIDGE", {"l2": 100.0}),
              ("BOOSTED_STUMPS", {"n_estimators": 60, "learning_rate": 0.05, "loss": "squared"}))
CALIBRATIONS = ("PLATT", "IDENTITY")
AUTHORITIES = ("PROB_AND_NET", "NET_ONLY")              # NET_ONLY: min_p = 0 (probability never authorizes)
SELECTION_CRITERIA = ("MAX_MEAN_R", "MAX_LOWER_1SE")
EDGE_GRID = tr.EDGE_GRID
P_GRID = tr.P_GRID

# research-only (never promotable)
RESEARCH_RANK_TARGETS = ("MFE_R_16", "MFE_MINUS_HALF_MAE_16", "RANK_NET_WITHIN_MONTH_REGIME")
CONDITIONAL_SPLITS = ("direction", "regime_group", "vol_high_low", "major_vs_alt")
RESEARCH_FILTERS = {"cost_to_reward_le_0_25": ("cost_to_reward", "<=", 0.25),
                    "h4_trend_agree": ("h4_trend_agree", ">", 0.0),
                    "h1_ema_align": ("h1_ema_align", ">", 0.0),
                    "atr_rank_mid_20_80": ("atr_pct_rank", "between", (0.2, 0.8)),
                    "vol_z_gt_0": ("vol_z", ">", 0.0)}
EXIT_TIME_BARS = (4, 8, 16)
EXIT_TARGETS_R = (1.0, 1.5, 2.0)

# supporting gate (Part M) - fixed before the run
MIN_POOLED_TRADES = 60
MAX_TOP_SYMBOL_SHARE = 0.5
MAX_TOP_MONTH_SHARE = 0.5
MAX_TOP_REGIME_SHARE = 0.6
MAX_BRIER_EXCESS_OVER_BASE = 0.0            # probability authority requires beating base rate
MAX_ECE = 0.10
REQUIRED_COST_SCENARIOS = ("fees_plus_50pct", "slippage_x2", "combined_adverse")

SPEC = {
    "version": VERSION, "evidence_label": EVIDENCE_LABEL, "forward_evidence_cutoff_ms": FORWARD_EVIDENCE_CUTOFF_MS,
    "horizon_bars_15m": list(HORIZON_BARS), "majors": list(MAJORS),
    "promotable": {"class_targets": list(CLASS_TARGETS), "regressor_target": "GROSS_R (runtime subtracts costs)",
                   "classifiers": [list(c) for c in CLASSIFIERS], "regressors": [list(r) for r in REGRESSORS],
                   "calibrations": list(CALIBRATIONS), "authorities": list(AUTHORITIES),
                   "selection_criteria": list(SELECTION_CRITERIA), "p_grid": list(P_GRID),
                   "edge_grid": list(EDGE_GRID), "features": "RUNTIME_SCHEMA_ONLY",
                   "feature_schema_sha256": fx.schema_hash(),
                   "net_only_rule": "min_p_profitable=0: the classifier never authorizes; net edge decides",
                   "prob_rule": "PROB_AND_NET only when inner-validation calibration beats base-rate Brier"},
    "fail_closed_rules": {"min_validation_trades": tr.MIN_VALIDATION_TRADES, "min_group_trades": tr.MIN_GROUP_TRADES,
                          "direction_regime_requires_positive_validation_mean_r": True,
                          "never_tradable_regimes": ["UNKNOWN", "EXTREME"]},
    "research_only": {"rank_targets": list(RESEARCH_RANK_TARGETS), "conditional_splits": list(CONDITIONAL_SPLITS),
                      "filters": {k: list(v) for k, v in RESEARCH_FILTERS.items()},
                      "research_features": list(rfx.RESEARCH_FEATURES),
                      "research_feature_version": rfx.RESEARCH_FEATURE_VERSION,
                      "exit_time_bars": list(EXIT_TIME_BARS), "exit_targets_r": list(EXIT_TARGETS_R),
                      "exit_intrabar_rule": "stop and target in the same bar => stop first (conservative)"},
    "walk_forward": {"folds": 4, "outer_steps": [{"train": [1], "validate": 2, "evaluate": 3},
                                                 {"train": [1, 2], "validate": 3, "evaluate": 4}],
                     "purge_horizon": "max(dependence horizon, 16 bars forward path)"},
    "supporting_gate": {"non_abstain_both_steps": True, "min_pooled_trades": MIN_POOLED_TRADES,
                        "expectancy_authority_valid_ci_low_gt_0": True,
                        "uplift_vs_hook_baseline_authority_valid_ci_low_gt_0": True,
                        "cost_stress_positive": list(REQUIRED_COST_SCENARIOS),
                        "max_top_symbol_share": MAX_TOP_SYMBOL_SHARE, "max_top_month_share": MAX_TOP_MONTH_SHARE,
                        "max_top_regime_share": MAX_TOP_REGIME_SHARE,
                        "stability": "final spec+criterion non-ABSTAIN with positive inner score in step 1; "
                                     "selected policies overlap in direction and regime",
                        "calibration_if_probability_authorizes": {"beats_base_rate": True, "max_ece": MAX_ECE},
                        "deterministic_export_load_inference": True, "runtime_feature_parity": True},
}
SPEC_SHA256 = hashlib.sha256(json.dumps(SPEC, sort_keys=True).encode()).hexdigest()
TARGET_SPEC_SHA256 = hashlib.sha256(json.dumps(
    {"class_targets": CLASS_TARGETS, "regressor": "GROSS_R", "horizon_bars": HORIZON_BARS},
    sort_keys=True).encode()).hexdigest()
COST_POLICY = {"decision_cost_r": "training.cost_estimate_r(cost_fraction, stop_distance_pct)",
               "uncertainty_buffer_r": "DecisionPolicy.uncertainty_buffer_r",
               "realized_costs": "replay production-parity fees, slippage, funding",
               "stress": list(REQUIRED_COST_SCENARIOS)}
COST_POLICY_SHA256 = hashlib.sha256(json.dumps(COST_POLICY, sort_keys=True).encode()).hexdigest()


class ForwardEvidenceRefused(ValueError):
    pass


# ── generic helpers ─────────────────────────────────────────────────────────
def _fi(name):
    return fx.MODEL_FEATURES.index(name)


def feat(row, name):
    v = (row.get("ai_features") or [None] * len(fx.MODEL_FEATURES))[_fi(name)]
    return None if v is None else float(v)


def stats(rs) -> dict:
    """n, mean, median, win rate, profit factor, payoff, total, IID bootstrap CI (diagnostic)."""
    from bot import nexus_oos_research as res
    x = [float(v) for v in rs if v is not None]
    n = len(x)
    if not n:
        return {"n": 0}
    wins = [v for v in x if v > 0]
    losses = [-v for v in x if v < 0]
    out = {"n": n, "mean_r": sum(x) / n, "median_r": float(np.median(x)), "win_rate": len(wins) / n,
           "profit_factor": (sum(wins) / sum(losses)) if losses else None,
           "payoff_ratio": ((sum(wins) / len(wins)) / (sum(losses) / len(losses))) if wins and losses else None,
           "total_r": sum(x)}
    if n >= 20:
        lo, hi = res.bootstrap_mean_ci(x, samples=500)
        out["iid_ci"] = [lo, hi]
        out["ci_role"] = "IID_DIAGNOSTIC_ONLY"
    return out


def quantile_bucket(values, v, q=5):
    if v is None:
        return "MISSING"
    qs = np.quantile([x for x in values if x is not None], np.linspace(0, 1, q + 1)[1:-1])
    k = int(np.searchsorted(qs, v, side="right"))
    return f"Q{k + 1}"


def check_no_forward_evidence(rows):
    bad = [r for r in rows if int(r["ts"]) >= FORWARD_EVIDENCE_CUTOFF_MS]
    if bad:
        raise ForwardEvidenceRefused(f"{len(bad)} rows at/after the forward-evidence cutoff")


# ── forward path metrics (outcome side only) ───────────────────────────────
def path_metrics(row) -> dict | None:
    fwd = row.get("_fwd") or []
    entry, stop = row.get("entry_fill"), row.get("stop")
    if len(fwd) < 2 or not entry or stop is None or entry == stop:
        return None
    s = 1.0 if str(row.get("direction")).upper() == "LONG" else -1.0
    risk = abs(float(entry) - float(stop))
    cost = tr.cost_estimate_r(row.get("cost_fraction") or 0.0, risk / float(entry))
    out = {"cost_r_est": cost}
    mfe = mae = 0.0
    t_mfe = t_mae = 0
    first = {"+0.5": None, "+1": None, "-0.5": None, "stop": None}
    for k, (_, o, h, lo, c) in enumerate(fwd[:max(HORIZON_BARS)], start=1):
        fav = s * ((h if s > 0 else lo) - float(entry)) / risk
        adv = s * ((lo if s > 0 else h) - float(entry)) / risk
        if adv < mae:
            mae, t_mae = adv, k
        if fav > mfe:
            mfe, t_mfe = fav, k
        # adverse first within a bar (conservative)
        for key, lvl in (("-0.5", -0.5), ("stop", -1.0)):
            if first[key] is None and adv <= lvl:
                first[key] = k
        for key, lvl in (("+0.5", 0.5), ("+1", 1.0)):
            if first[key] is None and fav >= lvl:
                first[key] = k
        if k in HORIZON_BARS:
            out[f"h{k}"] = {"gross_r": s * (c - float(entry)) / risk, "net_r": s * (c - float(entry)) / risk - cost,
                            "mfe_r": mfe, "mae_r": mae, "t_mfe": t_mfe, "t_mae": t_mae}
    def before(a, b):
        return first[a] is not None and (first[b] is None or first[a] < first[b])
    out["first_touch"] = {"plus05_before_minus05": before("+0.5", "-0.5"), "plus1_before_stop": before("+1", "stop"),
                          "stop_first": first["stop"] is not None and not before("+1", "stop"), **first}
    out["mfe1_before_mae1"] = before("+1", "stop")
    return out


def attach_paths(data):
    for r in data:
        r["_pm"] = path_metrics(r)


# ── A: diagnostics ─────────────────────────────────────────────────────────
DIAG_FEATURE_KEYS = ("atr_pct_14", "realized_vol_32", "adx_14", "rsi_14", "ema20_ema50_gap", "ema20_slope_8",
                     "ret_16", "volume_multiple_20", "range_position_32", "h1_trend_slope_12", "h4_trend_slope_6",
                     "stop_distance_pct", "cost_fraction", "planned_rr", "strategy_score", "nexus_confidence")


def diagnostics(data) -> dict:
    def seg(keyfn):
        g = defaultdict(list)
        for r in data:
            g[str(keyfn(r))].append(r["r"])
        return {k: stats(v) for k, v in sorted(g.items())}
    out = {"baseline": stats([r["r"] for r in data]), "role": "DIAGNOSTIC_ONLY_NOT_A_SELECTION"}
    import datetime as dt
    cats = {"symbol": lambda r: r["symbol"], "direction": lambda r: r["direction"],
            "ai_regime": lambda r: r.get("ai_regime"), "production_regime": lambda r: r.get("production_regime"),
            "entry_type": lambda r: r.get("entry_type"), "session": lambda r: r.get("session"),
            "exit_reason": lambda r: r.get("exit_reason"), "month": lambda r: r.get("month"),
            "hour_utc": lambda r: dt.datetime.utcfromtimestamp(int(r["ts"]) / 1000).hour,
            "weekday": lambda r: dt.datetime.utcfromtimestamp(int(r["ts"]) / 1000).strftime("%a"),
            "major_vs_alt": lambda r: "MAJOR" if r["symbol"] in MAJORS else "ALT",
            "funding_sign": lambda r: "NONE" if not r.get("funding_r") else ("PAID" if r["funding_r"] < 0 else "RECEIVED"),
            "geometry_status": lambda r: r.get("geometry_status")}
    out["categorical"] = {k: seg(f) for k, f in cats.items()}
    num = {k: (lambda r, k=k: feat(r, k)) for k in DIAG_FEATURE_KEYS}
    num.update({"holding_time_h": lambda r: (r.get("outcome_horizon_ms") or 0) / 3_600_000,
                "rr_signal": lambda r: r.get("rr"), "fees_r": lambda r: r.get("fees_r"),
                "slippage_r": lambda r: r.get("slippage_r"), "funding_r": lambda r: r.get("funding_r"),
                "mfe_r_16": lambda r: (r["_pm"] or {}).get("h16", {}).get("mfe_r") if r.get("_pm") else None,
                "mae_r_16": lambda r: (r["_pm"] or {}).get("h16", {}).get("mae_r") if r.get("_pm") else None})
    for k in rfx.RESEARCH_FEATURES:
        num[f"rf_{k}"] = (lambda r, k=k: (r.get("rf") or {}).get(k))
    quint = {}
    for k, f in num.items():
        vals = [f(r) for r in data]
        if sum(v is not None for v in vals) < 50:
            quint[k] = {"status": "INSUFFICIENT_VALUES"}
            continue
        quint[k] = seg(lambda r, f=f, vals=vals: quantile_bucket(vals, f(r)))
    out["quintiles"] = quint
    return out


# ── B: loss attribution ────────────────────────────────────────────────────
def attribution(data) -> dict:
    n = len(data)
    net = [r["r"] for r in data]
    gross = [r.get("gross_r") for r in data if r.get("gross_r") is not None]
    fees = [r.get("fees_r") or 0.0 for r in data]
    slip = [r.get("slippage_r") or 0.0 for r in data]
    fund = [r.get("funding_r") or 0.0 for r in data]
    pm = [r for r in data if r.get("_pm")]
    losers = [r for r in pm if r["r"] < 0]
    stopped = [r for r in losers if str(r.get("exit_reason", "")).upper().find("SL") >= 0
               or str(r.get("exit_reason", "")).upper().find("STOP") >= 0]
    dir_right_4h = [r for r in pm if (r["_pm"].get("h16") or {}).get("gross_r", 0) > 0]
    gave_back = [r for r in pm if (r["_pm"].get("h16") or {}).get("mfe_r", 0) >= 1.0 and r["r"] <= 0]
    stop_then_1r = [r for r in stopped if (r["_pm"].get("h16") or {}).get("mfe_r", 0) >= 1.0]
    low_rr = [r for r in data if (r.get("rr") or 0) < 2.0]
    comp = {
        "net_mean_r": float(np.mean(net)) if net else None,
        "gross_mean_r": float(np.mean(gross)) if gross else None,
        "fees_mean_r": float(np.mean(fees)), "slippage_mean_r": float(np.mean(slip)),
        "funding_mean_r": float(np.mean(fund)),
        "cost_share_of_net_loss": (float(np.mean(fees) + np.mean(slip) + np.mean(fund)) / float(np.mean(net)))
        if net and np.mean(net) < 0 else None,
        "direction_hit_rate_4h": len(dir_right_4h) / len(pm) if pm else None,
        "direction_hit_rate_1h": (sum(1 for r in pm if (r["_pm"].get("h4") or {}).get("gross_r", 0) > 0) / len(pm))
        if pm else None,
        "losers": len(losers), "losers_stopped": len(stopped),
        "stopped_then_reached_plus1r_within_4h": len(stop_then_1r),
        "reached_plus1r_but_closed_le_0": len(gave_back),
        "low_rr_lt2_n": len(low_rr), "low_rr_mean_r": float(np.mean([r["r"] for r in low_rr])) if low_rr else None,
    }
    def contrib(keyfn):
        g = defaultdict(float)
        for r in data:
            g[str(keyfn(r))] += r["r"]
        tot = sum(v for v in g.values() if v < 0)
        return {k: {"total_r": v, "share_of_losses": (v / tot) if tot < 0 and v < 0 else 0.0}
                for k, v in sorted(g.items(), key=lambda kv: kv[1])}
    vol = [feat(r, "atr_pct_14") for r in data]
    comp["by_symbol"] = contrib(lambda r: r["symbol"])
    comp["by_regime"] = contrib(lambda r: r.get("ai_regime"))
    comp["by_volatility_quintile"] = contrib(lambda r: quantile_bucket(vol, feat(r, "atr_pct_14")))
    nc = [r.get("nexus_confidence") for r in data]
    comp["by_nexus_confidence_quintile"] = contrib(lambda r: quantile_bucket(nc, r.get("nexus_confidence")))
    # predeclared verdict rules
    verdict = []
    if comp["gross_mean_r"] is not None and comp["gross_mean_r"] <= 0:
        verdict.append("SIGNAL")                      # no gross edge before costs
    elif comp["gross_mean_r"] is not None and comp["net_mean_r"] < 0:
        verdict.append("COST")                         # gross edge eaten by costs
    hr = comp["direction_hit_rate_4h"]
    if hr is not None and hr < 0.55 and "SIGNAL" in verdict:
        verdict.append("NO_EDGE_PRESENT_IN_DIRECTION")
    if stopped and len(stop_then_1r) / len(stopped) > 0.35:
        verdict.append("RISK_STOP_TOO_TIGHT_CANDIDATE")
    if pm and len(gave_back) / len(pm) > 0.10:
        verdict.append("EXIT_GIVEBACK_CANDIDATE")
    comp["verdict"] = verdict
    comp["verdict_rules"] = {"SIGNAL": "gross mean R <= 0", "COST": "gross > 0 but net < 0",
                             "NO_EDGE_PRESENT_IN_DIRECTION": "SIGNAL and 4h direction hit-rate < 0.55",
                             "RISK_STOP_TOO_TIGHT_CANDIDATE": ">35% of stopped losers reach +1R within 4h",
                             "EXIT_GIVEBACK_CANDIDATE": ">10% reach +1R then close <= 0"}
    comp["n"] = n
    return comp


# ── C: horizons ────────────────────────────────────────────────────────────
def horizons(data) -> dict:
    pm = [r["_pm"] for r in data if r.get("_pm")]
    out = {"rows_with_path": len(pm), "role": "DIAGNOSTIC_ONLY"}
    for h in HORIZON_BARS:
        xs = [p[f"h{h}"] for p in pm if f"h{h}" in p]
        out[f"{h * 15}m"] = {"net_r": stats([x["net_r"] for x in xs]), "gross_r": stats([x["gross_r"] for x in xs]),
                             "mfe_r_mean": float(np.mean([x["mfe_r"] for x in xs])) if xs else None,
                             "mae_r_mean": float(np.mean([x["mae_r"] for x in xs])) if xs else None,
                             "direction_hit_rate": (sum(1 for x in xs if x["gross_r"] > 0) / len(xs)) if xs else None}
    ft = [p["first_touch"] for p in pm]
    out["first_touch"] = {k: (sum(1 for f in ft if f[k]) / len(ft)) if ft else None
                          for k in ("plus05_before_minus05", "plus1_before_stop", "stop_first")}
    return out


# ── E: feature diagnostics ─────────────────────────────────────────────────
def feature_diagnostics(data, folds) -> dict:
    X = np.array([r["ai_features"] for r in data], float)
    y = np.array([r["r"] for r in data], float)
    out = {"runtime_features": {}, "research_features": {}, "high_correlation_pairs": []}
    first, last = folds[0], folds[-1]
    for j, name in enumerate(fx.MODEL_FEATURES):
        col = X[:, j]
        fin = np.isfinite(col)
        c = col[fin]
        d = {"finite_fraction": float(fin.mean()), "unique": int(len(np.unique(c))),
             "near_constant": bool(len(c) and np.mean(c == np.median(c)) > 0.95)}
        if len(c) > 10:
            d.update({"min": float(c.min()), "max": float(c.max()), "p01": float(np.quantile(c, .01)),
                      "p99": float(np.quantile(c, .99))})
            d["spearman_net_r"] = _spearman(col[fin], y[fin])
            d["decile_mean_net_r"] = _decile_means(col[fin], y[fin])
            a = np.array([r["ai_features"][j] for r in first], float)
            b = np.array([r["ai_features"][j] for r in last], float)
            sd = float(np.nanstd(c)) or 1.0
            d["drift_f1_to_f4_sd"] = float((np.nanmean(b) - np.nanmean(a)) / sd) if len(a) and len(b) else None
            d["mutual_info_win_bits"] = _mi(col[fin], y[fin] > 0)
        out["runtime_features"][name] = d
    cm = np.corrcoef(np.nan_to_num(X).T)
    for i, j in itertools.combinations(range(len(fx.MODEL_FEATURES)), 2):
        if np.isfinite(cm[i, j]) and abs(cm[i, j]) > 0.9:
            out["high_correlation_pairs"].append([fx.MODEL_FEATURES[i], fx.MODEL_FEATURES[j], float(cm[i, j])])
    for k in rfx.RESEARCH_FEATURES:
        vals = [(r.get("rf") or {}).get(k) for r in data]
        pairs = [(v, r["r"]) for v, r in zip(vals, data) if v is not None]
        d = {"available": len(pairs), "runtime_parity": False, "promotable": False}
        if len(pairs) > 50:
            v, t = np.array([p[0] for p in pairs]), np.array([p[1] for p in pairs])
            d.update({"spearman_net_r": _spearman(v, t), "decile_mean_net_r": _decile_means(v, t),
                      "mutual_info_win_bits": _mi(v, t > 0)})
        out["research_features"][k] = d
    return out


def _spearman(a, b):
    if len(a) < 3:
        return None
    ra, rb = np.argsort(np.argsort(a)), np.argsort(np.argsort(b))
    c = np.corrcoef(ra, rb)[0, 1]
    return float(c) if np.isfinite(c) else None


def _decile_means(x, y):
    qs = np.quantile(x, np.linspace(0, 1, 11))
    out = []
    for lo, hi in zip(qs[:-1], qs[1:]):
        m = (x >= lo) & (x <= hi)
        out.append(float(y[m].mean()) if m.any() else None)
    return out


def _mi(x, win):
    qs = np.unique(np.quantile(x, np.linspace(0, 1, 11)[1:-1]))
    b = np.searchsorted(qs, x)
    mi = 0.0
    pw = float(np.mean(win))
    for k in np.unique(b):
        m = b == k
        pk = float(m.mean())
        for w, pwk in ((True, pw), (False, 1 - pw)):
            pj = float(np.mean(m & (win == w)))
            if pj > 0 and pk > 0 and pwk > 0:
                mi += pj * math.log2(pj / (pk * pwk))
    return mi


# ── I: exit research (paired; identical population) ───────────────────────
def exit_research(data) -> dict:
    pop = [r for r in data if r.get("_pm") and len(r.get("_fwd") or []) >= 17]
    variants = {"current": [r["r"] for r in pop]}
    for name in ("no_partial_tp", "no_trailing", "no_rr_double"):
        v = [(r.get("exit_r") or {}).get(name) for r in pop]
        if all(x is not None for x in v):
            variants[f"replay_{name}"] = v
    for t in EXIT_TIME_BARS:
        variants[f"time_exit_{t * 15}m_with_stop"] = [_sim_exit(r, None, t) for r in pop]
    for tgt in EXIT_TARGETS_R:
        variants[f"target_{tgt}R_stop_1R_4h"] = [_sim_exit(r, tgt, max(EXIT_TIME_BARS)) for r in pop]
    base = np.array(variants["current"])
    out = {"population": len(pop), "role": "RESEARCH_ONLY_NOT_APPLIED",
           "note": "production exits unchanged; fwd-path variants use decision-time cost estimate, no funding (<=4h)"}
    for k, v in variants.items():
        v = np.array(v, float)
        out[k] = {**stats(list(v)), "paired_delta_vs_current": float(np.mean(v - base)) if len(v) else None}
    return out


def _sim_exit(row, target_r, max_bars):
    s = 1.0 if str(row["direction"]).upper() == "LONG" else -1.0
    entry, stop = float(row["entry_fill"]), float(row["stop"])
    risk = abs(entry - stop)
    cost = row["_pm"]["cost_r_est"]
    last = None
    for (_, o, h, lo, c) in row["_fwd"][:max_bars]:
        adv = s * ((lo if s > 0 else h) - entry) / risk
        fav = s * ((h if s > 0 else lo) - entry) / risk
        if adv <= -1.0:
            return -1.0 - cost
        if target_r is not None and fav >= target_r:
            return target_r - cost
        last = s * (c - entry) / risk
    return (last if last is not None else 0.0) - cost


# ── targets (outcome side, training only) ──────────────────────────────────
def class_label(row, target) -> float:
    r = float(row["r"])
    if target == "NET_GT_0":
        return float(r > 0)
    if target == "NET_GT_025":
        return float(r > 0.25)
    if target == "HIGH_QUALITY_NET_GT_05":
        return float(r > 0.5)
    if target == "MFE1_BEFORE_MAE1":
        return float(bool((row.get("_pm") or {}).get("mfe1_before_mae1")))
    raise ValueError(target)


def promotable_specs():
    for tgt, (ck, chp), (rk, rhp), c, auth in itertools.product(CLASS_TARGETS, CLASSIFIERS, REGRESSORS,
                                                                CALIBRATIONS, AUTHORITIES):
        if auth == "NET_ONLY" and c == "PLATT":
            continue                      # probability is inert under NET_ONLY: calibration choice irrelevant
        yield {"target": tgt, "classifier": [ck, chp], "regressor": [rk, rhp], "calibration": c, "authority": auth}


def spec_id(spec) -> str:
    return hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()[:12]


def fit(spec, rows):
    X, _, _ = tr._xy(rows)
    y = np.array([class_label(r, spec["target"]) for r in rows])
    clf = tr._make(spec["classifier"][0], spec["classifier"][1], "A").fit(X, y)
    reg = tr._make(spec["regressor"][0], spec["regressor"][1], "B").fit(X, tr._gross_y(rows))
    return clf, reg


def calibrate(spec, clf, rows):
    if spec["calibration"] == "IDENTITY":
        return cal.Identity()
    X, _, _ = tr._xy(rows)
    return cal.Platt().fit(clf.predict_proba(X), np.array([class_label(r, spec["target"]) for r in rows]))


def _score(appr, criterion):
    rs = [float(r["r"]) for r in appr]
    if len(rs) < tr.MIN_VALIDATION_TRADES:
        return None
    m = float(np.mean(rs))
    return m if criterion == "MAX_MEAN_R" else m - float(np.std(rs, ddof=1)) / math.sqrt(len(rs))


def select_policy(val, p, er, criterion, *, authority, calibration_ok):
    """Grid cell by criterion; direction/regime gating = unchanged fail-closed rule."""
    if authority == "PROB_AND_NET" and not calibration_ok:
        return ABSTAIN_ALL, None, "CALIBRATION_DOES_NOT_BEAT_BASE_RATE"
    pgrid = P_GRID if authority == "PROB_AND_NET" else (0.0,)
    best = None
    for pth in pgrid:
        for edge in EDGE_GRID:
            pol = DecisionPolicy(pth, edge, allowed_directions=(LONG, SHORT), supported_regimes=TRADABLE_REGIMES)
            s = _score(tr.approve(val, p, er, pol), criterion)
            if s is not None and (best is None or s > best[0]):
                best = (s, pth, edge)
    if best is None:
        return ABSTAIN_ALL, None, "NO_GRID_CELL_WITH_MIN_TRADES"
    _, pth, edge = best
    dirs = [d for d in (LONG, SHORT) if tr._status(tr.approve(val, p, er, DecisionPolicy(
        pth, edge, allowed_directions=(d,), supported_regimes=TRADABLE_REGIMES))) == tr.ENABLED]
    regs = [g for g in TRADABLE_REGIMES if dirs and tr._status(tr.approve(val, p, er, DecisionPolicy(
        pth, edge, allowed_directions=tuple(dirs), supported_regimes=(g,)))) == tr.ENABLED]
    if not dirs or not regs:
        return ABSTAIN_ALL, None, "NO_DIRECTION_OR_REGIME_WITH_POSITIVE_VALIDATION_R"
    pol = DecisionPolicy(pth, edge, allowed_directions=tuple(dirs), supported_regimes=tuple(regs),
                         probability_authorizes=True)
    return pol, _score(tr.approve(val, p, er, pol), criterion), "SELECTED_ON_VALIDATION"


def _mean(rows):
    return float(np.mean([float(r["r"]) for r in rows])) if rows else None


def make_folds(data, required_ms):
    horizon = max(int(required_ms), max(HORIZON_BARS) * BAR_MS)
    lay = inf.purged_calendar_folds(int(data[0]["ts"]), int(data[-1]["ts"]) + 1, required_horizon_ms=horizon)
    if lay["status"] != "OK":
        return None, lay
    W = lay["windows"]
    folds = [[r for r in data if w["decision_start_ts"] <= int(r["ts"]) < w["decision_end_ts"]
              and max(int(r["outcome_end_ts"]), int(r["ts"]) + max(HORIZON_BARS) * BAR_MS) < w["outcome_window_end_ts"]]
             for w in W]
    return folds, lay


def search(data, folds) -> dict:
    specs = list(promotable_specs())
    ledger, steps = [], []
    for si, (tr_idx, va, ev) in enumerate((([0], 1, 2), ([0, 1], 2, 3))):
        train = [r for k in tr_idx for r in folds[k]]
        val, test = folds[va], folds[ev]
        best = None
        for spec in specs:
            clf, reg = fit(spec, train)
            calib = calibrate(spec, clf, val)
            Xv, _, _ = tr._xy(val)
            Xt, _, _ = tr._xy(test)
            p_val = list(calib.transform(clf.predict_proba(Xv)))
            er_val = list(reg.predict(Xv))
            p_te = list(calib.transform(clf.predict_proba(Xt)))
            er_te = list(reg.predict(Xt))
            yv = np.array([class_label(r, spec["target"]) for r in val])
            vrep = cal.report(np.array(p_val), yv)
            for crit in SELECTION_CRITERIA:
                pol, vscore, reason = select_policy(val, p_val, er_val, crit, authority=spec["authority"],
                                                    calibration_ok=bool(vrep["beats_base_rate"]))
                appr_val = tr.approve(val, p_val, er_val, pol)
                appr_te = tr.approve(test, p_te, er_te, pol)
                ledger.append({"step": si + 1, "candidate_id": f"{spec_id(spec)}-{crit}", "spec_id": spec_id(spec),
                               "spec": spec, "criterion": crit, "search_spec_sha256": SPEC_SHA256,
                               "policy": pol.to_json(), "policy_sha256": pol.sha256,
                               "abstain_all": pol.sha256 == ABSTAIN_ALL.sha256, "reason": reason,
                               "validation": {"n": len(appr_val), "mean_r": _mean(appr_val), "score": vscore,
                                              "brier": vrep["brier"], "brier_base": vrep["brier_base_rate"],
                                              "beats_base_rate": vrep["beats_base_rate"]},
                               "outer_posthoc_not_used_for_selection": {"n": len(appr_te), "mean_r": _mean(appr_te)},
                               "selected": False, "promotable": True})
                if vscore is not None and (best is None or vscore > best[0]):
                    best = (vscore, spec, crit, pol, appr_te, p_te, len(ledger) - 1, len(appr_val))
        if best is None:
            steps.append({"step": si + 1, "selected": None, "reason": "ALL_CANDIDATES_ABSTAIN_ON_VALIDATION",
                          "test": test, "approved": []})
            continue
        vscore, spec, crit, pol, appr_te, p_te, li, nval = best
        ledger[li]["selected"] = True
        yt = np.array([class_label(r, spec["target"]) for r in test])
        steps.append({"step": si + 1, "selected": {"candidate_id": f"{spec_id(spec)}-{crit}", "spec_id": spec_id(spec),
                                                   "spec": spec, "criterion": crit, "policy": pol.to_json(),
                                                   "policy_sha256": pol.sha256, "validation_score": vscore,
                                                   "validation_trades": nval},
                      "test": test, "approved": appr_te, "test_calibration": cal.report(np.array(p_te), yt),
                      "probability_authorizes": spec["authority"] == "PROB_AND_NET"})
    return {"ledger": ledger, "steps": steps, "candidates": len(specs) * len(SELECTION_CRITERIA)}


def research_leads(data, folds) -> dict:
    """RESEARCH-ONLY (never frozen): rank targets, research features, filters
    and conditional models, each evaluated with the same inner-select /
    outer-evaluate protocol (top validation quintile score)."""
    out = {"role": "RESEARCH_ONLY_NOT_PROMOTABLE", "rank_targets": {}, "filters": {}, "conditional_models": {}}
    def rank_target(r, name):
        pm = r.get("_pm") or {}
        h = pm.get("h16") or {}
        if name == "MFE_R_16":
            return h.get("mfe_r")
        if name == "MFE_MINUS_HALF_MAE_16":
            return None if not h else h["mfe_r"] - 0.5 * abs(h["mae_r"])
        return float(r["r"])
    for name in RESEARCH_RANK_TARGETS:
        res = []
        for tr_idx, va, ev in (([0], 1, 2), ([0, 1], 2, 3)):
            train = [r for k in tr_idx for r in folds[k] if rank_target(r, name) is not None]
            if len(train) < 100:
                continue
            X, _, _ = tr._xy(train)
            yv = np.array([rank_target(r, name) for r in train])
            if name == "RANK_NET_WITHIN_MONTH_REGIME":
                yv = _group_rank(train)
            m = tr._make("RIDGE", {"l2": 100.0}, "B").fit(X, yv)
            Xv, _, _ = tr._xy(folds[va])
            cut = float(np.quantile(m.predict(Xv), 0.8)) if len(folds[va]) else 0.0
            Xt, _, _ = tr._xy(folds[ev])
            top = [r for r, s in zip(folds[ev], m.predict(Xt)) if s >= cut]
            res.append({"outer_top_quintile": stats([r["r"] for r in top]), "outer_all": stats([r["r"] for r in folds[ev]])})
        out["rank_targets"][name] = res
    for name, (key, op, thr) in RESEARCH_FILTERS.items():
        def keep(r, key=key, op=op, thr=thr):
            v = (r.get("rf") or {}).get(key)
            if v is None:
                return False
            return (v <= thr) if op == "<=" else (v > thr) if op == ">" else (thr[0] <= v <= thr[1])
        out["filters"][name] = [{"fold": k + 1, "kept": stats([r["r"] for r in f if keep(r)]),
                                 "removed": stats([r["r"] for r in f if not keep(r)])} for k, f in enumerate(folds)]
    for split in CONDITIONAL_SPLITS:
        out["conditional_models"][split] = _conditional(folds, split)
    return out


def _group_rank(rows):
    g = defaultdict(list)
    for i, r in enumerate(rows):
        g[(r.get("month"), r.get("ai_regime"))].append(i)
    y = np.zeros(len(rows))
    for idx in g.values():
        vals = np.array([rows[i]["r"] for i in idx])
        ranks = np.argsort(np.argsort(vals)) / max(1, len(idx) - 1)
        for i, rk in zip(idx, ranks):
            y[i] = rk
    return y


def _split_key(r, split):
    if split == "direction":
        return r["direction"]
    if split == "regime_group":
        return {"TREND_UP": "TREND", "TREND_DOWN": "TREND"}.get(r.get("ai_regime"), r.get("ai_regime"))
    if split == "vol_high_low":
        v = feat(r, "atr_pct_14")
        return "HIGH" if v is not None and v >= 0.004 else "LOW"
    return "MAJOR" if r["symbol"] in MAJORS else "ALT"


def _conditional(folds, split):
    res = []
    for tr_idx, va, ev in (([0], 1, 2), ([0, 1], 2, 3)):
        train = [r for k in tr_idx for r in folds[k]]
        test = folds[ev]
        X, _, _ = tr._xy(train)
        glob = tr._make("RIDGE", {"l2": 100.0}, "B").fit(X, tr._gross_y(train))
        Xt, _, _ = tr._xy(test)
        gs = glob.predict(Xt)
        cs = np.array(gs, float)
        groups = defaultdict(list)
        for i, r in enumerate(train):
            groups[_split_key(r, split)].append(i)
        for key, idx in groups.items():
            if len(idx) < 4 * tr.MIN_GROUP_TRADES:
                continue                                  # no tiny groups: keep the global model
            sub = [train[i] for i in idx]
            Xs, _, _ = tr._xy(sub)
            m = tr._make("RIDGE", {"l2": 100.0}, "B").fit(Xs, tr._gross_y(sub))
            ti = [i for i, r in enumerate(test) if _split_key(r, split) == key]
            if ti:
                cs[ti] = m.predict(Xt[ti])
        def top(scores):
            cut = float(np.quantile(scores, 0.8)) if len(scores) else 0.0
            return stats([r["r"] for r, s in zip(test, scores) if s >= cut])
        res.append({"global_top_quintile": top(gs), "conditional_top_quintile": top(cs)})
    return res


# ── M: supporting gate ─────────────────────────────────────────────────────
def stability(sel, ledger) -> dict:
    if not all(sel):
        return {"stable": False, "reason": "NO_SELECTION_IN_SOME_STEP"}
    fin = sel[-1]
    back = [e for e in ledger if e["step"] == 1 and e["candidate_id"] == fin["candidate_id"]]
    ok = bool(back) and not back[0]["abstain_all"] and (back[0]["validation"]["score"] or 0) > 0
    p1, p2 = sel[0]["policy"], fin["policy"]
    overlap = bool(set(p1["allowed_directions"]) & set(p2["allowed_directions"])) and bool(
        set(p1["supported_regimes"]) & set(p2["supported_regimes"]))
    return {"stable": ok and overlap, "final_spec_enabled_in_step1": ok, "policy_overlap": overlap}


def gate(steps, ledger, required_ms) -> dict:
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
    if not appr:
        fails.append("NO_APPROVED_TRADES")
        return {**rep, "failures": sorted(set(fails)), "all_pass": False, "label": EVIDENCE_LABEL}
    pooled = [dict(r, _a=id(r) in ids) for r in test]
    exp = inf.dependence_aware_mean(pooled, lambda r: r["_a"], required_ms=required_ms)
    up = inf.dependence_aware_diff(pooled, lambda r: r["_a"], lambda r: True, required_ms=required_ms)
    rep["expectancy"] = {k: exp.get(k) for k in ("mean_r", "n", "authority_status", "authority_ci_low",
                                                 "authority_ci_high", "iid_ci")}
    rep["uplift"] = {k: up.get(k) for k in ("delta", "authority_status", "authority_ci_low", "authority_ci_high",
                                            "iid_ci")}
    if not (exp.get("mean_r") or 0) > 0:
        fails.append("EXPECTANCY_NOT_POSITIVE")
    if not (exp.get("authority_status") == inf.AUTHORITY_VALID and (exp.get("authority_ci_low") or -1) > 0):
        fails.append("EXPECTANCY_CI_NOT_POSITIVE")
    if not (up.get("delta") or 0) > 0:
        fails.append("UPLIFT_NOT_POSITIVE")
    if not (up.get("authority_status") == inf.AUTHORITY_VALID and (up.get("authority_ci_low") or -1) > 0):
        fails.append("UPLIFT_CI_NOT_POSITIVE")
    stress = res.cost_stress(appr)
    rep["cost_stress"] = {k: (v or {}).get("net_expectancy_r") for k, v in stress.items()}
    for sc in REQUIRED_COST_SCENARIOS:
        if not (rep["cost_stress"].get(sc) or -1) > 0:
            fails.append(f"COST_STRESS_FAILS_{sc.upper()}")
    conc = {k: res.concentration(appr, k) for k in ("symbol", "month", "ai_regime", "direction")}
    rep["concentration"] = conc
    for k, lim, code in (("symbol", MAX_TOP_SYMBOL_SHARE, "SINGLE_SYMBOL_DOMINATES"),
                         ("month", MAX_TOP_MONTH_SHARE, "SINGLE_MONTH_DOMINATES"),
                         ("ai_regime", MAX_TOP_REGIME_SHARE, "SINGLE_REGIME_DOMINATES")):
        if (conc[k].get("top_share_of_positive_r") or 1.0) > lim:
            fails.append(code)
    rep["calibration"] = [s.get("test_calibration") for s in steps]
    if any(s.get("probability_authorizes") for s in steps):
        for c in rep["calibration"]:
            if c is None or not c.get("beats_base_rate") or (c.get("ece") or 1) > MAX_ECE:
                fails.append("CALIBRATION_FAILURE_WITH_PROBABILITY_AUTHORITY")
    return {**rep, "failures": sorted(set(fails)), "all_pass": not fails, "label": EVIDENCE_LABEL}


# ── orchestration ──────────────────────────────────────────────────────────
def run(rows, *, required_ms: int) -> dict:
    check_no_forward_evidence(rows)
    data = tr.dataset(rows)
    attach_paths(data)
    out = {"version": VERSION, "evidence_label": EVIDENCE_LABEL, "spec_sha256": SPEC_SHA256,
           "target_spec_sha256": TARGET_SPEC_SHA256, "cost_policy_sha256": COST_POLICY_SHA256,
           "dataset_manifest": tr.dataset_manifest(rows), "rows": len(data)}
    if len(data) < 200:
        return {**out, "status": "INSUFFICIENT_DATA", "result": "NO_VALID_CHALLENGER"}
    folds, lay = make_folds(data, required_ms)
    if folds is None:
        return {**out, "status": "INSUFFICIENT_INDEPENDENT_FOLDS", "result": "NO_VALID_CHALLENGER", "layout": lay}
    out["diagnostics"] = diagnostics(data)
    out["attribution"] = attribution(data)
    out["horizons"] = horizons(data)
    out["feature_diagnostics"] = feature_diagnostics(data, folds)
    out["exit_research"] = exit_research(data)
    out["research_leads"] = research_leads(data, folds)
    s = search(data, folds)
    out["ledger"] = s["ledger"]
    out["candidates_evaluated"] = s["candidates"]
    elig = [e for e in s["ledger"] if e["outer_posthoc_not_used_for_selection"]["n"] >= tr.MIN_GROUP_TRADES]
    best_ph = max(elig, key=lambda e: e["outer_posthoc_not_used_for_selection"]["mean_r"], default=None)
    out["best_posthoc_outer"] = None if best_ph is None else {
        "candidate_id": best_ph["candidate_id"], "step": best_ph["step"],
        **best_ph["outer_posthoc_not_used_for_selection"],
        "note": "data-snooping diagnostic only; never used for selection or freeze"}
    out["steps"] = [{k: v for k, v in st.items() if k not in ("test", "approved")}
                    | {"outer_eval": {"n_candidates": len(st["test"]), "hook_baseline_mean_r": _mean(st["test"]),
                                      "approved_n": len(st["approved"]), "approved_mean_r": _mean(st["approved"])}}
                    for st in s["steps"]]
    out["gate"] = gate(s["steps"], s["ledger"], required_ms)
    out["result"] = "CANDIDATE_SUPPORTED" if out["gate"]["all_pass"] else "NO_VALID_CHALLENGER"
    days = sum((int(f[-1]["ts"]) - int(f[0]["ts"])) / 86_400_000.0 for f in folds[2:] if f)
    out["estimate"] = {"outer_eval_days": days, "approved": out["gate"]["pooled_approved"]["n"],
                       "estimated_trades_per_72h": (out["gate"]["pooled_approved"]["n"] / days * 3) if days else None}
    out["_selected"] = s["steps"][-1]["selected"] if s["steps"] else None
    out["_folds"] = folds
    return {**out, "status": "OK"}


def runtime_parity(spec) -> dict:
    """Promotable candidates use only the runtime feature schema; the runtime
    computes exactly fx.MODEL_FEATURES via bot.ai.hook.features()."""
    return {"feature_schema_sha256": fx.schema_hash(), "features": list(fx.MODEL_FEATURES),
            "research_features_used": False, "parity": True}


def freeze(rows, result, *, training_code_sha: str, created_at: str) -> dict:
    from bot import nexus_oos_replay_manifest as rm
    from bot.ai import decision as dec
    from bot.ai import models as mdl
    if result.get("result") != "CANDIDATE_SUPPORTED":
        raise ValueError("no supported candidate; refusing to freeze")
    sel = result["_selected"]
    spec = sel["spec"]
    folds = result["_folds"]
    dev = folds[0] + folds[1] + folds[2]
    clf, reg = fit(spec, dev)
    calibrator = calibrate(spec, clf, folds[3])
    policy = DecisionPolicy.from_json(sel["policy"])
    ds = tr.dataset_manifest(rows)
    exit_sha = rm.canonical_sha256(rm.load().exit_policy().to_dict())
    common = dict(feature_names=fx.MODEL_FEATURES, feature_schema_sha256=fx.schema_hash(), training_manifest=ds,
                  training_code_sha=training_code_sha, created_at=created_at,
                  training_period={"first_ts": dev[0]["ts"], "last_ts": dev[-1]["ts"],
                                   "calibration_last_ts": folds[3][-1]["ts"] if folds[3] else None})
    ca = mdl.artifact(clf, role="A", hyperparameters={**spec["classifier"][1], "target": spec["target"],
                                                      "authority": spec["authority"]},
                      calibration=calibrator.to_json(), **common)
    ra = mdl.artifact(reg, role="B", hyperparameters=spec["regressor"][1], **common)
    bundle = dec.bundle_manifest(
        classifier_artifact=ca, regressor_artifact=ra, calibration=calibrator.to_json(), policy=policy,
        training_code_sha=training_code_sha, dataset_manifest_sha256=ds["sha256"],
        training_period=common["training_period"],
        selection_evidence={"version": VERSION, "search_spec_sha256": SPEC_SHA256,
                            "target_spec_sha256": TARGET_SPEC_SHA256, "cost_policy_sha256": COST_POLICY_SHA256,
                            "exit_policy_sha256": exit_sha, "candidate_id": sel["candidate_id"],
                            "evidence_label": EVIDENCE_LABEL, "runtime_parity": runtime_parity(spec)},
        created_at=created_at, lifecycle_state="SHADOW_CHALLENGER")
    return {"created": True, "lifecycle_state": "SHADOW_CHALLENGER", "bundle": bundle, "classifier_artifact": ca,
            "regressor_artifact": ra, "bundle_sha256": bundle["bundle_sha256"],
            "decision_policy_sha256": policy.sha256, "feature_schema_sha256": fx.schema_hash(),
            "exit_policy_sha256": exit_sha,
            "claims": {"edge_claim": False, "order_authority": False, "live_authority": False}}


def _public(res):
    return {k: v for k, v in res.items() if not k.startswith("_") and k != "ledger"}


def main(argv=None) -> int:
    import argparse
    from pathlib import Path
    from bot import nexus_oos_replay_manifest as rm
    from bot.ai import bundle_export as bx
    ap = argparse.ArgumentParser(description="Phase 8D signal discovery (research only)")
    ap.add_argument("--rows", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--candidate-sha", required=True)
    ap.add_argument("--created-at", required=True)
    a = ap.parse_args(argv)
    with gzip.open(a.rows, "rt", encoding="utf-8") as fh:
        header = json.loads(fh.readline())["header"]
        rows = [json.loads(line) for line in fh if line.strip()]
    res = run(rows, required_ms=int(header["required_ms"]))
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    def dump(name, obj):
        (out / name).write_text(json.dumps(obj, indent=1, sort_keys=True, default=str) + "\n")
    dump("search_spec.json", {"spec": SPEC, "spec_sha256": SPEC_SHA256, "target_spec_sha256": TARGET_SPEC_SHA256,
                              "cost_policy": COST_POLICY, "cost_policy_sha256": COST_POLICY_SHA256})
    for key, name in (("diagnostics", "signal_diagnostics.json"), ("attribution", "loss_attribution.json"),
                      ("horizons", "horizon_analysis.json"), ("feature_diagnostics", "feature_diagnostics.json"),
                      ("exit_research", "exit_research.json"), ("research_leads", "research_leads.json")):
        dump(name, res.get(key))
    with gzip.open(out / "candidate_ledger.json.gz", "wt", encoding="utf-8") as fh:
        fh.write(json.dumps(res.get("ledger") or [], sort_keys=True, default=str))
    summary = {k: res.get(k) for k in ("version", "evidence_label", "spec_sha256", "target_spec_sha256",
                                       "cost_policy_sha256", "rows", "status", "result", "candidates_evaluated",
                                       "best_posthoc_outer", "steps", "gate", "estimate", "dataset_manifest")}
    summary.update({"replay_header": header, "replay_policy_manifest_sha256": rm.load().sha256,
                    "candidate_code_sha": a.candidate_sha,
                    "attribution_verdict": (res.get("attribution") or {}).get("verdict"),
                    "baseline": (res.get("diagnostics") or {}).get("baseline")})
    if res.get("result") == "CANDIDATE_SUPPORTED":
        ch = freeze(rows, res, training_code_sha=a.candidate_sha, created_at=a.created_at)
        art = {"candidate_sha": a.candidate_sha, "candidate_research": {"ai_meta_model": {
            "shadow_challenger": ch, "dataset_manifest": tr.dataset_manifest(rows)}}}
        exp = bx.export(art, out / "challenger_bundle", candidate_sha=a.candidate_sha)
        v1, v2 = bx.verify(out / "challenger_bundle"), bx.verify(out / "challenger_bundle")
        summary["frozen"] = {"bundle_sha256": ch["bundle_sha256"], "policy": ch["bundle"]["decision_policy"],
                             "decision_policy_sha256": ch["decision_policy_sha256"],
                             "feature_schema_sha256": ch["feature_schema_sha256"],
                             "exit_policy_sha256": ch["exit_policy_sha256"], "file_sha256": exp["file_sha256"],
                             "deterministic": v1["deterministic_inference"] == v2["deterministic_inference"],
                             "verify": v1}
    dump("challenger_summary.json", summary)
    print(json.dumps({k: summary.get(k) for k in ("result", "rows", "candidates_evaluated", "attribution_verdict",
                                                  "baseline", "best_posthoc_outer", "estimate")},
                     indent=1, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
