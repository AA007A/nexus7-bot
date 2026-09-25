"""Phase 8G-B: INCREMENTAL_ALPHA_SPEC_V1 - funding + basis + cross-sectional (research only).

Question: do KuCoin settled funding and/or same-venue perp/index-close basis add
out-of-sample NET trading value relative to an otherwise identical PRICE-ONLY
control? Everything below is predeclared in SPEC and hashed before any target is
evaluated. Evidence label: PREVIOUSLY_INSPECTED_RESEARCH_SUPPORT.

Design
  rows      : every 4h UTC boundary T x symbol in the common window where PRICE,
              XSEC, FUNDING and BASIS features are ALL genuinely available and all
              targets are resolvable (identical rows for every ablation; no imputation)
  trade     : enter at the open of the 1h bar starting at T, stop = 1.5 * h4 ATR14,
              time exit after H hours (12/24/48); LONG and SHORT evaluated separately
  realized  : fees + slippage + ACTUAL settled funding in (entry, exit] signed by side
              + 0.05R buffer. Funding as a predictive feature is separate from the
              realized funding cost (never double counted)
  models    : RIDGE(l2=10), RIDGE(l2=100) on net R; LOGISTIC(l2=1)+Platt on net R > 0
              (probability authorizes only if validation calibration beats base rate)
  thresholds: ridge predicted net R > {0, 0.05, 0.10}; logistic p >= {0.50, 0.55, 0.60}
  ablations : A..H promotable, I..K diagnostic; identical grid for every set
  uplift    : paired per decision slot vs the SAME config trained on the matched
              no-exogenous control (C,D,E -> A ; F,G,H -> B)
"""
from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import json
import math
from collections import defaultdict

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view as _swv

from bot import nexus_oos_inference as inf
from bot.ai import calibration as cal
from bot.ai import exo_features as ex
from bot.ai import lh_features as lf
from bot.ai import models as mdl
from bot.ai import xsec_features as xf

EVIDENCE_LABEL = "PREVIOUSLY_INSPECTED_RESEARCH_SUPPORT"
VERSION = "INCREMENTAL_ALPHA_SPEC_V1"
H1, DAY = 3_600_000, 86_400_000
WINDOW_START_MS, WINDOW_END_MS = 1_740_409_200_000, 1_787_756_400_000        # 8F / 8G-A audited window
DECISION_STEP_H = 4
HORIZONS = (12, 24, 48)
STOP_H4_ATR_MULT = 1.5
MIN_STOP_FRAC, MAX_STOP_FRAC = 0.005, 0.10
TAKER_FEE, SLIP_MAJOR, SLIP_ALT = 0.0006, 0.0005, 0.0010
MAJORS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
BUFFER_R = 0.05
REQUIRED_MS = 3 * DAY
MIN_COMMON_DAYS = 365
EXPECTED = {"price_dataset_sha256": "773a97d430904a6ffd4f294470cb343c42894f1f027e588aed438c807c8ec3c1",
            "funding_dataset_sha256_prefix": "a5d9957c7e55daca", "index_dataset_sha256_prefix": "cef5c3c8c0a1e239",
            "alpha_data_contract_sha256": "e21132a89d9739e6acd66920a8ec595975ad295372f9b7d75a7f7fc4990ab235"}

PRICE_KEYS = lf.MODEL_FEATURES                                     # side-specific vector (h4 trigger)
PRICE_REQUIRED = ("h4_c", "h4_sma20", "h4_sma50", "h4_std20", "h4_atr14", "h4_donhi20", "h4_donlo20", "h4_atr_rank50",
                  "h4_roc10", "h4_range_ratio", "h4_body", "h4_trend", "h4_structure", "h4_range", "d1_trend",
                  "d1_range", "d1_structure", "d1_c", "d1_sma20", "d1_atr14")
XSEC_KEYS = ("rank_ret_24h", "rank_ret_168h", "rel_btc_24h", "rel_eth_24h", "rel_mkt_24h", "resid_24h", "leadlag",
             "mkt_ret_24h", "alt_mom_168h", "btc_trend", "breadth", "rank_rvol_24h", "beta_btc_168h", "corr_btc_168h",
             "dispersion_24h", "volume_shock_24h", "rvol_term_24_168")
XSEC_DIRECTIONAL = XSEC_KEYS[:11]
FAMILIES = {"P": PRICE_KEYS, "X": XSEC_KEYS, "F": ex.F_KEYS, "B": ex.B_KEYS}
SETS = {"A": ("P",), "B": ("P", "X"), "C": ("P", "F"), "D": ("P", "B"), "E": ("P", "F", "B"), "F": ("P", "X", "F"),
        "G": ("P", "X", "B"), "H": ("P", "X", "F", "B"), "I": ("F",), "J": ("B",), "K": ("F", "B")}
SET_NAMES = {"A": "PRICE_ONLY", "B": "PRICE+XSEC", "C": "PRICE+FUNDING", "D": "PRICE+BASIS", "E": "PRICE+FUNDING+BASIS",
             "F": "PRICE+XSEC+FUNDING", "G": "PRICE+XSEC+BASIS", "H": "PRICE+XSEC+FUNDING+BASIS",
             "I": "FUNDING_ONLY (diagnostic)", "J": "BASIS_ONLY (diagnostic)", "K": "FUNDING+BASIS (diagnostic)"}
PROMOTABLE = ("A", "B", "C", "D", "E", "F", "G", "H")
DIAGNOSTIC = ("I", "J", "K")
EXOGENOUS = ("C", "D", "E", "F", "G", "H")
CONTROL = {"C": "A", "D": "A", "E": "A", "F": "B", "G": "B", "H": "B"}
JOINT_SINGLE = {"E": ("C", "D"), "H": ("F", "G")}
SINGLE_SOURCE = ("C", "D", "F", "G")
MODELS = (("RIDGE", {"l2": 10.0}), ("RIDGE", {"l2": 100.0}), ("LOGISTIC", {"l2": 1.0}))
THRESH = {"RIDGE": (0.0, 0.05, 0.10), "LOGISTIC": (0.50, 0.55, 0.60)}
MIN_VALIDATION_TRADES = 30
MIN_VALIDATION_ROWS = 10 * MIN_VALIDATION_TRADES
STEPS = (([0], 1, 2), ([0, 1], 2, 3))
STRESS = ("fees_x1_5", "slippage_x2", "funding_x2", "combined_adverse")
GATE = {"min_pooled_trades": 60, "max_top_symbol_share": 0.5, "max_top_month_share": 0.5,
        "max_top_quarter_share": 0.6, "max_top_regime_share": 0.6, "max_top_week_share": 0.35, "max_ece": 0.10}


def configs():
    for H in HORIZONS:
        for kind, hp in MODELS:
            for th in THRESH[kind]:
                yield {"H": H, "model": kind, "hp": hp, "threshold": th}


def cfg_id(c):
    return f"H{c['H']}|{c['model']}|{json.dumps(c['hp'], sort_keys=True)}|{c['threshold']}"


N_CONFIGS = sum(1 for _ in configs())
SEARCH = {"promotable_configurations": len(PROMOTABLE) * N_CONFIGS, "diagnostic_configurations": len(DIAGNOSTIC) * N_CONFIGS}
SEARCH["total_configurations"] = SEARCH["promotable_configurations"] + SEARCH["diagnostic_configurations"]
assert SEARCH["promotable_configurations"] < 500 and SEARCH["total_configurations"] <= 1000

SPEC = {
    "version": VERSION, "evidence_label": EVIDENCE_LABEL, "expected_identities": EXPECTED,
    "eligible_sources": ["KUCOIN_FUNDING_SETTLED", "KUCOIN_PERP_INDEX_CLOSE_BASIS", "CROSS_SECTIONAL_FEATURES_V1",
                         "PHASE_8F_PRICE (LONG_HORIZON_FEATURES_V1 control)"],
    "excluded_sources": ["Binance/Bybit/OKX proxies", "open interest", "positioning", "liquidations", "taker flow",
                         "order book", "predicted funding", "live-only KuCoin snapshots"],
    "window": {"audited": [WINDOW_START_MS, WINDOW_END_MS], "decision_step_h": DECISION_STEP_H,
               "last_decision": "WINDOW_END - 49h (targets and realized funding inside the audited data)",
               "common_rows": "all families available + every target resolvable; identical rows for every ablation",
               "min_common_days": MIN_COMMON_DAYS},
    "features": {"price": {"schema": lf.VERSION, "schema_sha256": lf.SCHEMA_SHA256, "vector": list(PRICE_KEYS),
                           "trigger": "h4", "required_raw": list(PRICE_REQUIRED)},
                 "xsec": {"schema": xf.VERSION, "schema_sha256": xf.SCHEMA_SHA256, "used": list(XSEC_KEYS),
                          "transforms": {"rank_*": "minus 0.5", "leadlag": "leader - laggard", "btc_trend": "2x-1",
                                         "breadth": "breadth_sma480 - 0.5"},
                          "directional_by_side": list(XSEC_DIRECTIONAL)},
                 "exogenous": {"schema": ex.VERSION, "schema_sha256": ex.SCHEMA_SHA256}},
    "side_signing": "directional features are multiplied by side (+1 LONG / -1 SHORT); the model coefficient sign "
                    "therefore encodes continuation vs contrarian for every predeclared hypothesis",
    "hypotheses": {"funding": ["extreme positive funding = crowded longs (contrarian short)",
                               "extreme negative funding = crowded shorts (contrarian long)",
                               "funding persistence = positioning pressure (continuation)",
                               "funding/price divergence"],
                   "basis": ["unusually positive / negative perp_index_close_basis", "rapid expansion / compression",
                             "basis/price divergence"],
                   "xsec": ["relative-strength continuation", "leader/laggard reversion", "breadth-confirmed trend",
                            "dispersion regime"],
                   "note": "both continuation and contrarian interpretations are represented by the signed linear model"},
    "trade": {"entry": "open of the 1h bar starting at T", "stop": f"{STOP_H4_ATR_MULT} x h4 ATR14",
              "stop_frac_bounds": [MIN_STOP_FRAC, MAX_STOP_FRAC], "exit": "time exit at the close of hour H; stop first",
              "sizing": "fixed 1R capital at risk (notional = risk / stop_frac)",
              "overlap": "one open trade per symbol; higher score wins if both sides approve"},
    "targets": {f"NET_R_{H}H": "gross - fees - slippage - realized funding - buffer" for H in HORIZONS}
               | {"binary": "NET_R_H > 0 (LOGISTIC)"},
    "costs": {"taker_fee_per_side": TAKER_FEE, "slippage_per_side": {"majors": SLIP_MAJOR, "alts": SLIP_ALT},
              "realized_funding": "sum of settled rates in (entry, exit], LONG pays positive / SHORT receives",
              "buffer_r": BUFFER_R,
              "stress": {"fees_x1_5": "fees x1.5", "slippage_x2": "slippage x2",
                         "funding_x2": "adverse funding x2, received funding ignored",
                         "combined_adverse": "all three"}},
    "feature_sets": {k: {"families": list(v), "name": SET_NAMES[k]} for k, v in SETS.items()},
    "promotable_sets": list(PROMOTABLE), "diagnostic_sets": list(DIAGNOSTIC), "matched_control": CONTROL,
    "models": [[k, hp] for k, hp in MODELS], "thresholds": THRESH, "horizons": list(HORIZONS), "search": SEARCH,
    "walk_forward": {"folds": 4, "purge_horizon_ms": REQUIRED_MS, "steps": [{"train": [1], "validate": 2, "evaluate": 3},
                                                                            {"train": [1, 2], "validate": 3, "evaluate": 4}]},
    "selection": {"per_set": f"max validation mean net R per trade (>= {MIN_VALIDATION_TRADES} trades, > 0)",
                  "primary_exogenous": "among C..H configs eligible per_set rules: max validation paired uplift per "
                                       "decision slot vs the same config on the matched control; hierarchical "
                                       "parsimony: single-source sets (C, D, F, G) first; a joint set (E, H) is "
                                       "considered only if no single-source config is eligible in that step AND its "
                                       "validation uplift vs BOTH single-source sets (same config) is > 0",
                  "probability": "LOGISTIC configs ineligible unless validation Platt calibration beats base rate"},
    "gate": {**GATE, "expectancy_block_ci_low_gt_0": True, "uplift_block_ci_low_gt_0": True,
             "stress_positive": list(STRESS), "stable_incremental_sign_across_outer_folds": True,
             "joint_requirement": "if the selected set is E (H), uplift vs C and D (F and G) on the same config with "
                                  "block CI low > 0",
             "feature_parity": True, "data_identity": True},
    "classification": {"CANDIDATE_SUPPORTED": "an exogenous set (C..H) passes every gate",
                       "INSUFFICIENT_EVIDENCE": "data identity / alignment insufficient (common < 365 d, folds fail); "
                                                "or only sample-size failures (pooled < 60 or block CI not estimable) "
                                                "with positive point expectancy and uplift; or abstention while a "
                                                "validation fold has < 300 rows (a model declining to trade on "
                                                "adequate data is NO_INCREMENTAL_ALPHA, not missing evidence)",
                       "NO_INCREMENTAL_ALPHA": "otherwise (incl. XSEC-only improvements)"},
}
SPEC_SHA256 = hashlib.sha256(json.dumps(SPEC, sort_keys=True).encode()).hexdigest()
COST_POLICY_SHA256 = hashlib.sha256(json.dumps(SPEC["costs"], sort_keys=True).encode()).hexdigest()
EXIT_POLICY_SHA256 = hashlib.sha256(json.dumps(SPEC["trade"], sort_keys=True).encode()).hexdigest()
FEATURE_SCHEMA = {"price": SPEC["features"]["price"], "xsec": SPEC["features"]["xsec"], "exogenous": ex.SCHEMA,
                  "families": {k: list(v) for k, v in FAMILIES.items()}}
FEATURE_SCHEMA_SHA256 = hashlib.sha256(json.dumps(FEATURE_SCHEMA, sort_keys=True).encode()).hexdigest()


# ── dataset build ──────────────────────────────────────────────────────────
def _slip(sym):
    return SLIP_MAJOR if sym in MAJORS else SLIP_ALT


def _month(t):
    return dt.datetime.fromtimestamp(int(t) / 1000, dt.timezone.utc).strftime("%Y-%m")


def xsec_vector(M, s, q):
    g = lambda k: float(M["per_symbol"][s][k][q])            # noqa: E731
    m = lambda k: float(M["market"][k][q])                    # noqa: E731
    return [g("rank_ret_24h") - 0.5, g("rank_ret_168h") - 0.5, g("rel_btc_24h"), g("rel_eth_24h"), g("rel_mkt_24h"),
            g("resid_24h"), g("leader") - g("laggard"), m("mkt_ret_24h"), m("alt_mom_168h"),
            2 * m("btc_trend_sma480") - 1, m("breadth_sma480") - 0.5, g("rank_rvol_24h"), g("beta_btc_168h"),
            g("corr_btc_168h"), m("dispersion_24h"), g("volume_shock_24h"), g("rvol_term_24_168")]


def simulate_targets(arr, ts, idx, S, E, D, sf, settle_ts, settle_cum, sym):
    """Vectorized time-exit + stop simulation with REALIZED funding for entries at bar index idx."""
    o, h, lo, c = arr
    Hm = max(HORIZONS)
    Ow, Hw, Lw, Cw = (_swv(x, Hm)[idx] for x in (o, h, lo, c))
    Sx, Ex, Dx = S[:, None], E[:, None], D[:, None]
    adv = Sx * (np.where(Sx > 0, Lw, Hw) - Ex) / Dx
    oR = Sx * (Ow - Ex) / Dx
    cR = Sx * (Cw - Ex) / Dx
    hit = adv <= -1.0
    k_stop = np.where(hit.any(axis=1), hit.argmax(axis=1), 10 ** 6)
    rows = np.arange(len(idx))
    out = {}
    fee = 2 * TAKER_FEE / sf
    slip = 2 * _slip(sym) / sf
    for H in HORIZONS:
        stopped = k_stop < H
        ks = np.minimum(k_stop, H - 1)
        gross = np.where(stopped, np.minimum(-1.0, oR[rows, ks]), cR[:, H - 1])
        k = np.where(stopped, k_stop, H - 1)
        exit_ts = ts[idx + k] + H1
        a = np.searchsorted(settle_ts, ts[idx], side="right")            # settlements strictly after entry
        b = np.searchsorted(settle_ts, exit_ts, side="right")          # ... and at/before exit
        rate_sum = settle_cum[b] - settle_cum[a]
        fund = S * rate_sum / sf                                       # LONG pays positive rates
        out[H] = {"gross": gross, "fee": fee, "slip": slip, "fund": fund, "exit_ts": exit_ts,
                  "n_settle": (b - a).astype(float), "net": gross - fee - slip - fund - BUFFER_R}
    return out


def build_dataset(price: dict, funding: dict, index: dict, *, symbols=None, window=(WINDOW_START_MS, WINDOW_END_MS)) -> dict:
    symbols = sorted(symbols or price)
    S, E = window
    t_first = S - S % (DECISION_STEP_H * H1) + DECISION_STEP_H * H1
    t_last = E - (max(HORIZONS) + 1) * H1
    Tg = np.arange(t_first, t_last + 1, DECISION_STEP_H * H1, dtype=np.int64)
    sub = {s: [b for b in price[s] if S - 32 * DAY <= int(b["ts"]) < E + 3 * DAY] for s in symbols}
    M = xf.matrix(sub, Tg)
    cols = defaultdict(list)
    miss = defaultdict(int)
    eff = {"funding_settlements_in_window": 0, "basis_hours_in_window": 0}
    for s in symbols:
        bars = price[s]
        ts = np.array([int(b["ts"]) for b in bars], np.int64)
        arr = tuple(np.array([float(b[k]) for b in bars]) for k in ("o", "h", "l", "c"))
        F = lf.series(bars)
        pos = {int(t): i for i, t in enumerate(ts)}
        fl = sorted((int(t), v) for t, v in funding.get(s, []))
        f_ts = np.array([t for t, _ in fl], np.int64)
        f_rt = np.array([np.nan if v is None else float(v) for _, v in fl], float)
        eff["funding_settlements_in_window"] += int(((f_ts >= S) & (f_ts < E)).sum())
        ret24 = M["per_symbol"][s]["ret_24h"]
        fu = ex.funding_series(f_ts, f_rt, Tg, ret24)
        pc = {int(b["ts"]): float(b["c"]) for b in bars}
        ic = {int(t): float(c) for t, c in index.get(s, [])}
        hg = np.arange(S - S % H1, E, H1, dtype=np.int64)
        bh = ex.basis_hourly(hg, pc, ic)
        eff["basis_hours_in_window"] += int(np.isfinite(bh).sum())
        ba = ex.basis_series(hg, bh, Tg, ret24)
        for q, T in enumerate(Tg):
            i = pos.get(int(T))
            if i is None or i + max(HORIZONS) > len(ts) or ts[i + max(HORIZONS) - 1] != int(T) + (max(HORIZONS) - 1) * H1:
                miss["price_path_incomplete"] += 1
                continue
            if not all(np.isfinite(F[k][i]) for k in PRICE_REQUIRED):
                miss["price_features_missing"] += 1
                continue
            xv = xsec_vector(M, s, q)
            if not all(math.isfinite(v) for v in xv):
                miss["xsec_missing"] += 1
                continue
            fv = [fu[k][q] for k in ex.F_KEYS]
            if not all(math.isfinite(v) for v in fv):
                miss["funding_missing"] += 1
                continue
            bv = [ba[k][q] for k in ex.B_KEYS]
            if not all(math.isfinite(v) for v in bv):
                miss["basis_missing"] += 1
                continue
            e = arr[0][i]
            d = STOP_H4_ATR_MULT * float(F["h4_atr14"][i])
            sf = d / e
            if not (MIN_STOP_FRAC <= sf <= MAX_STOP_FRAC):
                miss["stop_out_of_bounds"] += 1
                continue
            cf = 2 * TAKER_FEE + 2 * _slip(s)
            cols["T"].append(int(T))
            cols["sym"].append(s)
            cols["i"].append(i)
            cols["e"].append(e)
            cols["d"].append(d)
            cols["sf"].append(sf)
            cols["regime"].append({1.0: "TREND_UP", -1.0: "TREND_DOWN"}.get(float(F["d1_trend"][i]),
                                                                           "RANGE" if F["d1_range"][i] == 1 else "MIXED"))
            cols["P_L"].append(lf.model_vector(F, i, 1.0, "h4", sf, cf))
            cols["P_S"].append(lf.model_vector(F, i, -1.0, "h4", sf, cf))
            cols["X"].append(xv)
            cols["F"].append(fv)
            cols["B"].append(bv)
    ds = {k: np.array(v) for k, v in cols.items() if k not in ("P_L", "P_S", "X", "F", "B")}
    for k in ("P_L", "P_S", "X", "F", "B"):
        ds[k] = np.array(cols[k], float).reshape(len(cols["T"]), -1) if cols["T"] else np.zeros((0, 1))
    ds["tgt"] = {}
    n = len(ds.get("T", []))
    for side, sv in (("L", 1.0), ("S", -1.0)):
        for H in HORIZONS:
            ds["tgt"][(side, H)] = {k: np.full(n, np.nan) for k in ("gross", "fee", "slip", "fund", "exit_ts", "net", "n_settle")}
    valid = np.ones(n, bool)
    for s in symbols:
        m = np.flatnonzero(ds["sym"] == s) if n else np.array([], int)
        if not len(m):
            continue
        bars = price[s]
        ts = np.array([int(b["ts"]) for b in bars], np.int64)
        arr = tuple(np.array([float(b[k]) for b in bars]) for k in ("o", "h", "l", "c"))
        fl = sorted((int(t), v) for t, v in funding.get(s, []))
        f_ts = np.array([t for t, _ in fl], np.int64)
        f_rt = np.array([np.nan if v is None else float(v) for _, v in fl], float)
        cum = np.concatenate([[0.0], np.cumsum(np.nan_to_num(f_rt, nan=0.0))])
        nan_cum = np.concatenate([[0], np.cumsum(~np.isfinite(f_rt))])
        last_settle = f_ts[-1] if len(f_ts) else 0
        for side, sv in (("L", 1.0), ("S", -1.0)):
            res = simulate_targets(arr, ts, ds["i"][m].astype(int), np.full(len(m), sv), ds["e"][m], ds["d"][m],
                                   ds["sf"][m], f_ts, cum, s)
            for H, r in res.items():
                a = np.searchsorted(f_ts, ts[ds["i"][m].astype(int)], side="right")
                b = np.searchsorted(f_ts, r["exit_ts"], side="right")
                bad_f = (nan_cum[b] - nan_cum[a]) > 0
                beyond = r["exit_ts"] > last_settle + 8 * H1
                valid[m[bad_f | beyond]] = False
                for k, v in r.items():
                    ds["tgt"][(side, H)][k][m] = v
    miss["funding_unknown_during_hold"] = int((~valid).sum())
    ds = subset(ds, np.flatnonzero(valid)) if n else ds
    ds["month"] = np.array([_month(t) for t in ds["T"]]) if len(ds.get("T", [])) else np.array([])
    ds["missing"] = dict(miss)
    ds["effective"] = eff
    ds["grid"] = [int(Tg[0]), int(Tg[-1])] if len(Tg) else None
    return ds


def subset(ds, idx):
    out = {}
    for k, v in ds.items():
        if k == "tgt":
            out[k] = {kk: {a: b[idx] for a, b in vv.items()} for kk, vv in v.items()}
        elif isinstance(v, np.ndarray) and len(v.shape) >= 1 and len(v) == len(ds["T"]):
            out[k] = v[idx]
        else:
            out[k] = v
    return out


def dataset_sha256(ds) -> str:
    h = hashlib.sha256()
    h.update(np.asarray(ds["T"], np.int64).tobytes())
    h.update("|".join(ds["sym"]).encode())
    for k in ("P_L", "P_S", "X", "F", "B", "sf"):
        h.update(np.round(np.asarray(ds[k], float), 12).tobytes())
    for key in sorted(ds["tgt"]):
        for k in ("gross", "fund", "net", "exit_ts"):
            h.update(np.round(np.asarray(ds["tgt"][key][k], float), 12).tobytes())
    return h.hexdigest()


# ── features per set / side ────────────────────────────────────────────────
_DIRMASK = {"X": np.array([k in XSEC_DIRECTIONAL for k in XSEC_KEYS]),
            "F": np.array([k in ex.F_DIRECTIONAL for k in ex.F_KEYS]),
            "B": np.array([k in ex.B_DIRECTIONAL for k in ex.B_KEYS])}


def design(ds, set_key, side: str, idx=None):
    fams = SETS[set_key]
    parts = []
    sgn = 1.0 if side == "L" else -1.0
    for f in fams:
        if f == "P":
            M = ds["P_L"] if side == "L" else ds["P_S"]
        else:
            M = ds[f] * np.where(_DIRMASK[f], sgn, 1.0)
        parts.append(M if idx is None else M[idx])
    return np.hstack(parts)


# ── models / policy ────────────────────────────────────────────────────────
class Fitted:
    def __init__(self, cfg, model, calibrator=None):
        self.cfg, self.model, self.calibrator = cfg, model, calibrator

    def score(self, X):
        if self.cfg["model"] == "RIDGE":
            return np.asarray(self.model.predict(X), float)
        p = np.asarray(self.model.predict_proba(X), float)
        return np.asarray(self.calibrator.transform(p), float) if self.calibrator is not None else p

    def to_json(self):
        return {"cfg": self.cfg, "model": self.model.params(),
                "calibration": self.calibrator.to_json() if self.calibrator is not None else None}

    @classmethod
    def from_json(cls, j):
        m = (mdl.Ridge if j["cfg"]["model"] == "RIDGE" else mdl.LogisticL2).from_params(j["model"])
        return cls(j["cfg"], m, cal.from_json(j["calibration"]) if j.get("calibration") else None)


def fit(ds, set_key, H, kind, hp, tr_idx, va_idx):
    X = np.vstack([design(ds, set_key, "L", tr_idx), design(ds, set_key, "S", tr_idx)])
    y = np.concatenate([ds["tgt"][("L", H)]["net"][tr_idx], ds["tgt"][("S", H)]["net"][tr_idx]])
    cfg0 = {"H": H, "model": kind, "hp": hp}
    if kind == "RIDGE":
        return Fitted(cfg0, mdl.Ridge(**hp).fit(X, y)), None
    yb = (y > 0).astype(float)
    if yb.min() == yb.max():
        return None, None
    m = mdl.LogisticL2(**hp).fit(X, yb)
    Xv = np.vstack([design(ds, set_key, "L", va_idx), design(ds, set_key, "S", va_idx)])
    yv = np.concatenate([ds["tgt"][("L", H)]["net"][va_idx], ds["tgt"][("S", H)]["net"][va_idx]]) > 0
    if not len(yv) or yv.all() or not yv.any():
        return Fitted(cfg0, m), None
    c = cal.Platt().fit(m.predict_proba(Xv), yv.astype(float))
    rep = cal.report(c.transform(m.predict_proba(Xv)), yv.astype(float))
    return Fitted(cfg0, m, c), rep


def _quarter(month):
    y, m = month.split("-")
    return f"{y}-Q{(int(m) - 1) // 3 + 1}"


def _week(t):
    d = dt.datetime.fromtimestamp(int(t) / 1000, dt.timezone.utc).isocalendar()
    return f"{d[0]}-W{d[1]:02d}"


def policy(ds, idx, sL, sS, H, kind, threshold) -> tuple[list, dict]:
    """Approve per side; one open trade per symbol; higher score wins; returns trades and per-slot R."""
    order = sorted(range(len(idx)), key=lambda q: (int(ds["T"][idx[q]]), ds["sym"][idx[q]]))
    busy = defaultdict(lambda: -1)
    trades, slots = [], defaultdict(float)
    for q in order:
        r = idx[q]
        T, s = int(ds["T"][r]), ds["sym"][r]
        slots[T] += 0.0
        ok = (lambda v: v > threshold) if kind == "RIDGE" else (lambda v: v >= threshold)
        cands = [(sL[q], "L"), (sS[q], "S")]
        cands = [c for c in cands if ok(c[0])]
        if not cands or T < busy[s]:
            continue
        sc, side = max(cands)
        t = ds["tgt"][(side, H)]
        fee, slip, fund, gross = t["fee"][r], t["slip"][r], t["fund"][r], t["gross"][r]
        fs = 2 * max(fund, 0.0)
        tr = {"ts": T, "end_ts": int(t["exit_ts"][r]), "outcome_end_ts": int(t["exit_ts"][r]), "symbol": s,
              "side": side, "r": float(t["net"][r]), "gross_r": float(gross), "fund_r": float(fund),
              "fees_x1_5": float(gross - 1.5 * fee - slip - fund - BUFFER_R),
              "slippage_x2": float(gross - fee - 2 * slip - fund - BUFFER_R),
              "funding_x2": float(gross - fee - slip - fs - BUFFER_R),
              "combined_adverse": float(gross - 1.5 * fee - 2 * slip - fs - BUFFER_R),
              "stop_frac": float(ds["sf"][r]), "hold_h": (int(t["exit_ts"][r]) - T) / H1,
              "month": ds["month"][r], "quarter": _quarter(ds["month"][r]), "week": _week(T),
              "regime": ds["regime"][r], "outcome_status": "RESOLVED"}
        trades.append(tr)
        slots[T] += tr["r"]
        busy[s] = tr["end_ts"]
    return trades, dict(slots)


def stats(x) -> dict:
    x = np.asarray(x, float)
    if not len(x):
        return {"n": 0}
    w, lo = x[x > 0], -x[x < 0]
    return {"n": int(len(x)), "mean_r": float(x.mean()), "median_r": float(np.median(x)), "win_rate": float((x > 0).mean()),
            "profit_factor": float(w.sum() / lo.sum()) if lo.sum() > 0 else None,
            "avg_win_r": float(w.mean()) if len(w) else None, "avg_loss_r": float(-lo.mean()) if len(lo) else None,
            "total_r": float(x.sum())}


def slot_diff(a: dict, b: dict) -> list:
    keys = sorted(set(a) | set(b))
    return [{"ts": T, "r": a.get(T, 0.0) - b.get(T, 0.0), "outcome_status": "RESOLVED"} for T in keys]


def ci_mean(rows) -> dict:
    if not rows:
        return {"mean_r": None, "authority_status": "NO_DATA"}
    d = inf.dependence_aware_mean(rows, required_ms=REQUIRED_MS)
    return {k: d.get(k) for k in ("mean_r", "n", "authority_status", "authority_ci_low", "authority_ci_high", "iid_ci")}


def max_drawdown(trades) -> float:
    eq = pk = dd = 0.0
    for t in sorted(trades, key=lambda t: (t["end_ts"], t["ts"])):
        eq += t["r"]
        pk = max(pk, eq)
        dd = max(dd, pk - eq)
    return dd


def share(vals, rs) -> dict:
    tot = defaultdict(float)
    for k, v in zip(vals, rs):
        tot[str(k)] += float(v)
    pos = {k: v for k, v in tot.items() if v > 0}
    ps = sum(pos.values())
    top = max(pos.items(), key=lambda kv: kv[1]) if pos else (None, 0.0)
    return {"groups": len(tot), "top_group": top[0], "top_share_of_positive_r": (top[1] / ps) if ps > 0 else None}


# ── experiment ─────────────────────────────────────────────────────────────
def folds(ds):
    if not len(ds["T"]):
        return None, {"status": "NO_ROWS"}
    lay = inf.purged_calendar_folds(int(ds["T"].min()), int(ds["T"].max()) + 1, required_horizon_ms=REQUIRED_MS)
    if lay["status"] != "OK":
        return None, lay
    f = np.full(len(ds["T"]), -1)
    for k, w in enumerate(lay["windows"]):
        end = np.maximum.reduce([ds["tgt"][(s, max(HORIZONS))]["exit_ts"] for s in ("L", "S")])
        m = (ds["T"] >= w["decision_start_ts"]) & (ds["T"] < w["decision_end_ts"]) & (end < w["outcome_window_end_ts"])
        f[m] = k
    return f, lay


def run_experiment(ds, *, parity_ok=True, identity_ok=True) -> dict:
    fold, lay = folds(ds)
    common_days = (float(ds["T"].max() - ds["T"].min()) / DAY) if len(ds["T"]) else 0.0
    base = {"status": "OK", "common_days": common_days, "rows": int(len(ds["T"]))}
    if not identity_ok:
        return {**base, "status": "DATA_IDENTITY_MISMATCH", "result": "INSUFFICIENT_EVIDENCE",
                "reason": "refetched funding/index datasets differ from the 8G-A audited identities"}
    if fold is None or common_days < MIN_COMMON_DAYS:
        return {**base, "status": "INSUFFICIENT_ALIGNED_HISTORY", "result": "INSUFFICIENT_EVIDENCE", "layout": lay,
                "reason": f"common aligned history {common_days:.1f} d (min {MIN_COMMON_DAYS}) or fold layout failed"}
    ledger, res = [], [defaultdict(dict), defaultdict(dict)]
    for si, (tr, va, ev) in enumerate(STEPS):
        tr_idx, va_idx, ev_idx = np.flatnonzero(np.isin(fold, tr)), np.flatnonzero(fold == va), np.flatnonzero(fold == ev)
        for sk in PROMOTABLE + DIAGNOSTIC:
            for H in HORIZONS:
                for kind, hp in MODELS:
                    fm, rep = fit(ds, sk, H, kind, hp, tr_idx, va_idx)
                    for th in THRESH[kind]:
                        c = {"H": H, "model": kind, "hp": hp, "threshold": th}
                        cid = cfg_id(c)
                        if fm is None:
                            res[si][sk][cid] = None
                            continue
                        calib_ok = kind == "RIDGE" or bool(rep and rep.get("beats_base_rate"))
                        out = {"cfg": c, "fitted": fm, "calib_ok": calib_ok, "val_calibration": rep}
                        for name, idx in (("val", va_idx), ("test", ev_idx)):
                            sL = fm.score(design(ds, sk, "L", idx))
                            sS = fm.score(design(ds, sk, "S", idx))
                            out[f"{name}_trades"], out[f"{name}_slots"] = policy(ds, idx, sL, sS, H, kind, th)
                            if name == "test" and kind == "LOGISTIC":
                                ys = np.concatenate([ds["tgt"][("L", H)]["net"][idx], ds["tgt"][("S", H)]["net"][idx]]) > 0
                                out["test_calibration"] = cal.report(np.concatenate([sL, sS]), ys.astype(float)) if len(ys) else None
                        vs = stats([t["r"] for t in out["val_trades"]])
                        out["eligible"] = bool(calib_ok and vs["n"] >= MIN_VALIDATION_TRADES and vs.get("mean_r", -1) > 0)
                        res[si][sk][cid] = out
                        ledger.append({"step": si + 1, "set": sk, "set_name": SET_NAMES[sk], "cfg": cid,
                                       "promotable": sk in PROMOTABLE, "search_spec_sha256": SPEC_SHA256,
                                       "calibration_ok": calib_ok,
                                       "validation": {**vs, "calibration": {k: (rep or {}).get(k) for k in
                                                                            ("brier", "brier_base_rate", "ece", "beats_base_rate")}},
                                       "outer_posthoc_not_used_for_selection": stats([t["r"] for t in out["test_trades"]]),
                                       "eligible": out["eligible"], "selected_per_set": False, "selected_primary": False})
    L = {(e["step"], e["set"], e["cfg"]): e for e in ledger}
    # per-set selection
    per_set = [dict(), dict()]
    for si in range(2):
        for sk in PROMOTABLE + DIAGNOSTIC:
            el = [(stats([t["r"] for t in o["val_trades"]])["mean_r"], cid) for cid, o in res[si][sk].items()
                  if o and o["eligible"]]
            if el:
                _, cid = max(el)
                per_set[si][sk] = cid
                L[(si + 1, sk, cid)]["selected_per_set"] = True
    # primary exogenous selection: validation paired uplift vs matched control (same config).
    # Hierarchical parsimony: single-source sets first; a joint set only if no single-source config is eligible.
    primary = []
    for si in range(2):
        best = None
        for tier in (SINGLE_SOURCE, tuple(JOINT_SINGLE)):
            for sk in tier:
                for cid, o in res[si][sk].items():
                    if not (o and o["eligible"]):
                        continue
                    ctrl = res[si][CONTROL[sk]].get(cid)
                    if not ctrl:
                        continue
                    if sk in JOINT_SINGLE:
                        singles = [res[si][x].get(cid) for x in JOINT_SINGLE[sk]]
                        if not all(z and np.mean([d["r"] for d in slot_diff(o["val_slots"], z["val_slots"])]) > 0
                                   for z in singles):
                            continue
                    up = float(np.mean([d["r"] for d in slot_diff(o["val_slots"], ctrl["val_slots"])]))
                    if best is None or (up, sk, cid) > best[:3]:
                        best = (up, sk, cid)
            if best is not None:
                break
        if best:
            L[(si + 1, best[1], best[2])]["selected_primary"] = True
        primary.append(best)
    out = {**base, "layout": {"status": lay["status"], "windows": lay["windows"]}, "ledger": ledger,
           "per_set_selection": [{k: v for k, v in p.items()} for p in per_set],
           "primary_selection": [None if p is None else {"set": p[1], "set_name": SET_NAMES[p[1]], "cfg": p[2],
                                                         "validation_uplift_per_slot": p[0],
                                                         "validation": stats([t["r"] for t in res[i][p[1]][p[2]]["val_trades"]])}
                                 for i, p in enumerate(primary)]}
    out["families"] = family_reports(res, per_set)
    g = gate(res, primary, parity_ok=parity_ok)
    out["gate"] = g
    val_rows = [int((fold == va).sum()) for _, va, _ in STEPS]
    out["validation_rows"] = val_rows
    out["result"], out["classification_reason"] = classify(g, primary, res, val_rows)
    out["posthoc_best_outer"] = posthoc(ledger)
    appr = g.get("_approved", [])
    days = sum((w["decision_end_ts"] - w["decision_start_ts"]) / DAY for w in lay["windows"][2:])
    out["estimate"] = {"outer_eval_days": days, "approved": len(appr),
                       "estimated_trades_per_72h": len(appr) / days * 3 if days else None}
    out["_res"], out["_primary"], out["_fold"] = res, primary, fold
    return out


def _pooled(res, sel_by_step):
    trades, slots_by_step = [], []
    for si, (sk, cid) in enumerate(sel_by_step):
        o = res[si][sk].get(cid)
        trades += o["test_trades"]
        slots_by_step.append(o["test_slots"])
    return trades, slots_by_step


def family_reports(res, per_set) -> dict:
    rep = {"per_set_outer": {}, "matched_uplift": {}}
    for sk in PROMOTABLE + DIAGNOSTIC:
        if all(sk in per_set[si] for si in range(2)):
            tr, _ = _pooled(res, [(sk, per_set[si][sk]) for si in range(2)])
            rep["per_set_outer"][sk] = {"name": SET_NAMES[sk], "configs": [per_set[si][sk] for si in range(2)],
                                        **stats([t["r"] for t in tr]), "expectancy_ci": ci_mean(tr)}
        else:
            rep["per_set_outer"][sk] = {"name": SET_NAMES[sk], "status": "ABSTAIN_IN_SOME_STEP"}
    pairs = {"FUNDING (C vs A)": ("C", "A"), "FUNDING (F vs B)": ("F", "B"), "BASIS (D vs A)": ("D", "A"),
             "BASIS (G vs B)": ("G", "B"), "JOINT (E vs A)": ("E", "A"), "JOINT (H vs B)": ("H", "B"),
             "JOINT_vs_FUNDING (E vs C)": ("E", "C"), "JOINT_vs_BASIS (E vs D)": ("E", "D"),
             "XSEC_PRICE_DERIVED (B vs A)": ("B", "A")}
    for name, (a, b) in pairs.items():
        if not all(a in per_set[si] for si in range(2)):
            rep["matched_uplift"][name] = {"status": f"{a} ABSTAINS IN SOME STEP"}
            continue
        diffs, per_fold = [], []
        for si in range(2):
            cid = per_set[si][a]
            oa, ob = res[si][a][cid], res[si][b].get(cid)
            if ob is None:
                continue
            d = slot_diff(oa["test_slots"], ob["test_slots"])
            diffs += d
            per_fold.append(float(np.mean([x["r"] for x in d])) if d else None)
        rep["matched_uplift"][name] = {"uplift_per_slot": ci_mean(diffs), "per_outer_fold": per_fold,
                                       "note": "same config trained on the control set; paired per decision slot"}
    return rep


def gate(res, primary, *, parity_ok) -> dict:
    fails = []
    if not parity_ok:
        fails.append("FEATURE_PARITY_FAILED")
    if not all(primary):
        return {"failures": sorted(set(fails + ["ABSTAIN_IN_SOME_STEP"])), "all_pass": False, "_approved": []}
    appr, diffs, ctrl_tr, per_fold = [], [], [], []
    for si, (_, sk, cid) in enumerate(primary):
        o, c = res[si][sk][cid], res[si][CONTROL[sk]][cid]
        appr += o["test_trades"]
        ctrl_tr += c["test_trades"]
        d = slot_diff(o["test_slots"], c["test_slots"])
        diffs += d
        per_fold.append(float(np.mean([x["r"] for x in d])) if d else 0.0)
    rs = [t["r"] for t in appr]
    g = {"selected": [{"set": p[1], "cfg": p[2]} for p in primary], "pooled_approved": stats(rs),
         "matched_control_outer": stats([t["r"] for t in ctrl_tr]), "expectancy": ci_mean(appr),
         "uplift_per_slot": ci_mean(diffs), "uplift_per_outer_fold": per_fold,
         "trade_count_change": len(appr) - len(ctrl_tr),
         "turnover_change_notional_per_risk": float(sum(1 / t["stop_frac"] for t in appr) - sum(1 / t["stop_frac"] for t in ctrl_tr)),
         "cost_change_r": float(sum(t["gross_r"] - t["r"] for t in appr) - sum(t["gross_r"] - t["r"] for t in ctrl_tr)),
         "max_drawdown_r": max_drawdown(appr), "control_max_drawdown_r": max_drawdown(ctrl_tr),
         "drawdown_change_r": max_drawdown(appr) - max_drawdown(ctrl_tr)}
    if len(appr) < GATE["min_pooled_trades"]:
        fails.append("TOO_FEW_POOLED_TRADES")
    e, u = g["expectancy"], g["uplift_per_slot"]
    if not (e.get("mean_r") or 0) > 0:
        fails.append("EXPECTANCY_NOT_POSITIVE")
    if e.get("authority_status") != inf.AUTHORITY_VALID:
        fails.append("EXPECTANCY_CI_NOT_ESTIMABLE")
    elif not (e.get("authority_ci_low") or -1) > 0:
        fails.append("EXPECTANCY_CI_NOT_POSITIVE")
    if not (u.get("mean_r") or 0) > 0:
        fails.append("UPLIFT_NOT_POSITIVE")
    if u.get("authority_status") != inf.AUTHORITY_VALID:
        fails.append("UPLIFT_CI_NOT_ESTIMABLE")
    elif not (u.get("authority_ci_low") or -1) > 0:
        fails.append("UPLIFT_CI_NOT_POSITIVE")
    if not all(x > 0 for x in per_fold):
        fails.append("INCREMENTAL_SIGN_UNSTABLE")
    joint = JOINT_SINGLE
    g["joint_vs_single_source"] = {}
    for alt in joint.get(primary[-1][1], ()):
        dd = []
        for si, (_, sk, cid) in enumerate(primary):
            if sk in joint and res[si][alt].get(cid):
                dd += slot_diff(res[si][sk][cid]["test_slots"], res[si][alt][cid]["test_slots"])
        ci = ci_mean(dd)
        g["joint_vs_single_source"][alt] = ci
        if not ((ci.get("mean_r") or 0) > 0 and ci.get("authority_status") == inf.AUTHORITY_VALID
                and (ci.get("authority_ci_low") or -1) > 0):
            fails.append(f"JOINT_NOT_BETTER_THAN_{alt}")
    g["cost_stress"] = {k: float(np.mean([t[k] for t in appr])) if appr else None for k in STRESS}
    for k, v in g["cost_stress"].items():
        if not (v is not None and v > 0):
            fails.append(f"COST_STRESS_FAILS_{k.upper()}")
    g["concentration"] = {k: share([t[k] for t in appr], rs) for k in ("symbol", "month", "quarter", "regime", "week")}
    for k, lim, code in (("symbol", GATE["max_top_symbol_share"], "SINGLE_SYMBOL_DOMINATES"),
                         ("month", GATE["max_top_month_share"], "SINGLE_MONTH_DOMINATES"),
                         ("quarter", GATE["max_top_quarter_share"], "SINGLE_QUARTER_DOMINATES"),
                         ("regime", GATE["max_top_regime_share"], "SINGLE_REGIME_DOMINATES"),
                         ("week", GATE["max_top_week_share"], "SINGLE_PERIOD_DOMINATES")):
        sh = g["concentration"][k]["top_share_of_positive_r"]
        if sh is None or sh > lim:
            fails.append(code)
    g["calibration"] = []
    for si, (_, sk, cid) in enumerate(primary):
        o = res[si][sk][cid]
        if o["cfg"]["model"] == "LOGISTIC":
            c = o.get("test_calibration") or {}
            g["calibration"].append({k: c.get(k) for k in ("brier", "brier_base_rate", "ece", "beats_base_rate")})
            if not c.get("beats_base_rate") or (c.get("ece") or 1) > GATE["max_ece"]:
                fails.append("CALIBRATION_FAILURE_WITH_PROBABILITY_AUTHORITY")
    g["failures"], g["all_pass"], g["_approved"] = sorted(set(fails)), not fails, appr
    return g


SAMPLE_FAILURES = {"TOO_FEW_POOLED_TRADES", "EXPECTANCY_CI_NOT_ESTIMABLE", "UPLIFT_CI_NOT_ESTIMABLE"}


def classify(g, primary, res, val_rows=(10 ** 9, 10 ** 9)) -> tuple[str, str]:
    if g["all_pass"]:
        return "CANDIDATE_SUPPORTED", "an exogenous set passes every incremental-alpha gate"
    f = set(g["failures"])
    if "ABSTAIN_IN_SOME_STEP" in f:
        if min(val_rows) < MIN_VALIDATION_ROWS:
            return "INSUFFICIENT_EVIDENCE", f"validation folds too small ({val_rows} rows < {MIN_VALIDATION_ROWS})"
        return "NO_INCREMENTAL_ALPHA", "no exogenous config is validation-eligible on adequate data (models decline to trade)"
    if f <= SAMPLE_FAILURES and (g["expectancy"].get("mean_r") or 0) > 0 and (g["uplift_per_slot"].get("mean_r") or 0) > 0:
        return "INSUFFICIENT_EVIDENCE", "only sample-size gate failures with positive point expectancy and uplift"
    return "NO_INCREMENTAL_ALPHA", "exogenous candidate fails economic / incremental / robustness gates"


def posthoc(ledger):
    el = [e for e in ledger if e["promotable"] and e["outer_posthoc_not_used_for_selection"].get("n", 0) >= MIN_VALIDATION_TRADES]
    b = max(el, key=lambda e: e["outer_posthoc_not_used_for_selection"]["mean_r"], default=None)
    return None if b is None else {"label": "POST_HOC_DIAGNOSTIC_ONLY", "step": b["step"], "set": b["set"],
                                   "cfg": b["cfg"], **b["outer_posthoc_not_used_for_selection"]}


def raw_diagnostics(ds, fold) -> dict:
    """DIAGNOSTIC_NOT_SELECTION_AUTHORITY: computed after the spec/dataset freeze; never used by selection."""
    out = {"label": "DIAGNOSTIC_NOT_SELECTION_AUTHORITY", "target": "NET_R_24H", "features": {}}
    for fam, keys in (("X", XSEC_KEYS), ("F", ex.F_KEYS), ("B", ex.B_KEYS)):
        for j, k in enumerate(keys):
            v = ds[fam][:, j]
            d = {}
            for side, sg in (("L", 1.0), ("S", -1.0)):
                y = ds["tgt"][(side, 24)]["net"]
                x = v * (sg if _DIRMASK[fam][j] else 1.0)
                per = []
                for f in range(4):
                    m = fold == f
                    per.append(_spearman(x[m], y[m]) if m.sum() > 30 else None)
                qs = np.quantile(x, [0.2, 0.4, 0.6, 0.8])
                b = np.searchsorted(qs, x, side="right")
                d[side] = {"spearman_by_fold": per,
                           "quintile_mean_net_r": [float(y[b == q].mean()) if (b == q).any() else None for q in range(5)]}
            out["features"][f"{fam}:{k}"] = d
    return out


def _spearman(a, b):
    ra, rb = np.argsort(np.argsort(a)), np.argsort(np.argsort(b))
    c = np.corrcoef(ra, rb)[0, 1] if len(a) > 2 else np.nan
    return float(c) if np.isfinite(c) else None


# ── freeze ─────────────────────────────────────────────────────────────────
PROBE_ROWS = 8


def _canon(o):
    return json.dumps(o, sort_keys=True, separators=(",", ":"), default=str).encode()


def freeze(result, ds, *, identities: dict, code_sha: str, created_at: str) -> dict:
    if result.get("result") != "CANDIDATE_SUPPORTED":
        raise ValueError("no supported candidate; refusing to freeze")
    _, sk, cid = result["_primary"][-1]
    o = result["_res"][1][sk][cid]
    fm = o["fitted"]
    rng = np.random.default_rng(0)
    probe = rng.normal(size=(PROBE_ROWS, design(ds, sk, "L", np.arange(1)).shape[1])).round(6)
    arch = {"feature_set": sk, "feature_set_name": SET_NAMES[sk], "families": list(SETS[sk]), "config": o["cfg"],
            "trade": SPEC["trade"], "decision_step_h": DECISION_STEP_H}
    policy_ = {"cfg_id": cid, "threshold": o["cfg"]["threshold"], "probability_authorizes": o["cfg"]["model"] == "LOGISTIC",
               "order_authority": False, "live_authority": False, "execution_lease": False, "exchange_credentials": None}
    model = fm.to_json()
    manifest = {"schema": "INCREMENTAL_ALPHA_BUNDLE_V1",
                "runtime_compatibility": "NOT AI_MODEL_BUNDLE_V1 and NOT LONG_HORIZON_ARCHITECTURE_BUNDLE_V1; "
                                         "requires a dedicated runtime integration",
                "lifecycle_state": "SHADOW_CHALLENGER", "evidence_label": EVIDENCE_LABEL,
                "search_spec_sha256": SPEC_SHA256, "feature_schema_sha256": FEATURE_SCHEMA_SHA256,
                "cost_policy_sha256": COST_POLICY_SHA256, "exit_policy_sha256": EXIT_POLICY_SHA256,
                "portfolio_policy_sha256": __import__("bot.ai.long_horizon", fromlist=["x"]).PORTFOLIO_POLICY_SHA256,
                **identities, "training_code_sha": code_sha, "created_at": created_at,
                "architecture_sha256": hashlib.sha256(_canon(arch)).hexdigest(),
                "policy_sha256": hashlib.sha256(_canon(policy_)).hexdigest(),
                "model_sha256": hashlib.sha256(_canon(model)).hexdigest(),
                "probe": probe.tolist(), "probe_scores": [float(v) for v in fm.score(probe)]}
    manifest["bundle_sha256"] = hashlib.sha256(_canon(manifest)).hexdigest()
    return {"architecture": arch, "policy": policy_, "model": model, "manifest": manifest}


def export(bundle, out_dir):
    from pathlib import Path
    p = Path(out_dir)
    p.mkdir(parents=True, exist_ok=True)
    shas = {}
    for n in ("architecture", "policy", "model", "manifest"):
        b = _canon(bundle[n])
        (p / f"{n}.json").write_bytes(b)
        shas[f"{n}.json"] = hashlib.sha256(b).hexdigest()
    return shas


def verify(out_dir) -> dict:
    from pathlib import Path
    p = Path(out_dir)
    b = {n: json.loads((p / f"{n}.json").read_text()) for n in ("architecture", "policy", "model", "manifest")}
    m = dict(b["manifest"])
    bsha = m.pop("bundle_sha256")
    fails = []
    if hashlib.sha256(_canon(m)).hexdigest() != bsha:
        fails.append("BUNDLE_SHA")
    for k, n in (("architecture_sha256", "architecture"), ("policy_sha256", "policy"), ("model_sha256", "model")):
        if hashlib.sha256(_canon(b[n])).hexdigest() != m[k]:
            fails.append(k.upper())
    if b["policy"].get("order_authority") or b["policy"].get("live_authority") or b["policy"].get("execution_lease"):
        fails.append("AUTHORITY_NOT_ALLOWED")
    fm = Fitted.from_json(b["model"])
    s1, s2 = fm.score(np.array(m["probe"])), fm.score(np.array(m["probe"]))
    det = bool(np.array_equal(s1, s2)) and np.allclose(s1, m["probe_scores"], rtol=0, atol=1e-12)
    if not det:
        fails.append("NON_DETERMINISTIC_INFERENCE")
    return {"verified": not fails, "failures": fails, "bundle_sha256": bsha, "schema": m["schema"],
            "lifecycle_state": m["lifecycle_state"], "deterministic_inference": det}


# ── parity over real data ──────────────────────────────────────────────────
def parity_report(price, funding, index, ds, *, per_symbol=40) -> dict:
    rep = {"funding": {}, "basis": {}, "tolerance": "relative 1e-9; NaN must equal NaN"}
    rng = np.random.default_rng(3)
    for s in sorted(price):
        m = np.flatnonzero(ds["sym"] == s)
        if not len(m):
            continue
        pick = np.sort(rng.choice(m, size=min(per_symbol, len(m)), replace=False))
        T = ds["T"][pick]
        pc = {int(b["ts"]): float(b["c"]) for b in price[s]}
        ret24 = np.array([math.log(pc[int(t) - H1] / pc[int(t) - 25 * H1]) if (int(t) - H1 in pc and int(t) - 25 * H1 in pc)
                          else float("nan") for t in T])
        fl = sorted((int(t), v) for t, v in funding.get(s, []))
        fr = ex.funding_series(np.array([t for t, _ in fl], np.int64), np.array([float(v) for _, v in fl]), T, ret24)
        rep["funding"][s] = ex.parity(fr, [(q, ex.funding_at(fl, int(t), float(ret24[q]))) for q, t in enumerate(T)], ex.F_KEYS)
        ic = {int(t): float(c) for t, c in index.get(s, [])}
        hg = np.arange(WINDOW_START_MS - WINDOW_START_MS % H1, WINDOW_END_MS, H1, dtype=np.int64)
        br = ex.basis_series(hg, ex.basis_hourly(hg, pc, ic), T, ret24)
        rep["basis"][s] = ex.parity(br, [(q, ex.basis_at(pc, ic, int(t), float(ret24[q]))) for q, t in enumerate(T)], ex.B_KEYS)
    sub = {s: [b for b in price[s] if WINDOW_START_MS - 32 * DAY <= int(b["ts"]) < WINDOW_END_MS] for s in price}
    rep["xsec"] = {k: v for k, v in xf.parity_report(sub, samples=80).items() if k != "examples"}
    rep["price"] = {s: {k: v for k, v in lf.parity_report(price[s], samples=40).items() if k != "examples"}
                    for s in sorted(price)}
    rep["parity"] = (all(v["parity"] for v in rep["funding"].values()) and all(v["parity"] for v in rep["basis"].values())
                     and rep["xsec"]["parity"] and all(v["parity"] for v in rep["price"].values()))
    return rep


# ── CLI ────────────────────────────────────────────────────────────────────
async def fetch_exogenous():
    from bot.ai import alpha_audit as aa
    S, E = aa.WINDOW_START_MS, aa.WINDOW_END_MS
    async with aa.Http() as http:
        c = await aa.kucoin_contracts(http)
        fund, idx = {}, {}
        for s in aa.SYMBOLS:
            rows, _ = await aa.kucoin_funding_history(http, s, S, E)
            fund[s] = rows
            isym = (c["symbols"].get(s) or {}).get("indexSymbol")
            idx[s] = (await aa.kucoin_klines(http, isym, S, E))[0] if isym else []
    kf, ki = {}, {}
    for s in aa.SYMBOLS:
        gran = int((c["symbols"].get(s) or {}).get("fundingRateGranularity") or aa.H8)
        recs = [aa.norm_kucoin_funding(r, symbol=s, granularity_ms=gran, retrieved_at=0) for r in fund[s]]
        kf[s] = [[r["event_ts"], r["value"]] for r in recs]
        ki[s] = [[b[0], b[4]] for b in idx.get(s, [])]
    return kf, ki


def canonical_sha(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def main(argv=None) -> int:
    import argparse
    import asyncio
    from pathlib import Path
    from bot.ai import alpha_audit as aa
    from bot.ai import long_horizon as lh
    ap = argparse.ArgumentParser(description="Phase 8G-B incremental alpha research (research only)")
    ap.add_argument("--price-dataset", required=True)
    ap.add_argument("--audit-manifests", default=None)
    ap.add_argument("--out", required=True)
    ap.add_argument("--code-sha", required=True)
    ap.add_argument("--created-at", required=True)
    a = ap.parse_args(argv)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    def dump(name, obj):
        (out / name).write_text(json.dumps(obj, indent=1, sort_keys=True, default=str) + "\n")
    dump("incremental_alpha_spec.json", {"spec": SPEC, "spec_sha256": SPEC_SHA256, "cost_policy_sha256": COST_POLICY_SHA256,
                                         "exit_policy_sha256": EXIT_POLICY_SHA256})
    price, hdr = lh.load_dataset(a.price_dataset)
    kf, ki = asyncio.run(fetch_exogenous())
    ids = {"price_dataset_sha256": hdr["sha256"], "funding_dataset_sha256": canonical_sha(kf),
           "index_dataset_sha256": canonical_sha(ki), "alpha_data_contract_sha256": aa.ALPHA_DATA_CONTRACT_SHA256,
           "xsec_schema_sha256": xf.SCHEMA_SHA256}
    audited = {}
    if a.audit_manifests and Path(a.audit_manifests).exists():
        audited = {m["dataset"]: m["sha256"] for m in json.loads(Path(a.audit_manifests).read_text())}
    checks = {"price": ids["price_dataset_sha256"] == EXPECTED["price_dataset_sha256"],
              "funding_prefix": ids["funding_dataset_sha256"].startswith(EXPECTED["funding_dataset_sha256_prefix"]),
              "index_prefix": ids["index_dataset_sha256"].startswith(EXPECTED["index_dataset_sha256_prefix"]),
              "funding_full_vs_8g_a": (audited.get("kucoin_funding_history") == ids["funding_dataset_sha256"]) if audited else None,
              "index_full_vs_8g_a": (audited.get("kucoin_index_kline_1h") == ids["index_dataset_sha256"]) if audited else None,
              "contract": ids["alpha_data_contract_sha256"] == EXPECTED["alpha_data_contract_sha256"]}
    identity_ok = all(v is not False for v in checks.values())
    dump("funding_dataset_manifest.json", {"sha256": ids["funding_dataset_sha256"], "rows": {s: len(v) for s, v in kf.items()},
                                           "first_ts": min((v[0][0] for v in kf.values() if v), default=None),
                                           "last_ts": max((v[-1][0] for v in kf.values() if v), default=None),
                                           "audited_8g_a": audited.get("kucoin_funding_history"), "identity_checks": checks,
                                           "definition": ex.SCHEMA["definitions"]["funding_rate_settled"]})
    dump("index_dataset_manifest.json", {"sha256": ids["index_dataset_sha256"], "rows": {s: len(v) for s, v in ki.items()},
                                         "audited_8g_a": audited.get("kucoin_index_kline_1h"),
                                         "definition": ex.SCHEMA["definitions"]["perp_index_close_basis"]})
    with gzip.open(out / "exogenous_canonical.json.gz", "wt") as fh:
        json.dump({"funding": kf, "index": ki}, fh, sort_keys=True)
    ds = build_dataset(price, kf, ki)
    dsha = dataset_sha256(ds) if len(ds["T"]) else None
    dump("feature_schema.json", {"schema": FEATURE_SCHEMA, "sha256": FEATURE_SCHEMA_SHA256})
    dump("alpha_dataset_manifest.json", {"name": "ALPHA_RESEARCH_DATASET_V1", "sha256": dsha, "rows": int(len(ds["T"])),
                                         "first_ts": int(ds["T"].min()) if len(ds["T"]) else None,
                                         "last_ts": int(ds["T"].max()) if len(ds["T"]) else None,
                                         "symbols": sorted(set(ds["sym"])) if len(ds["T"]) else [],
                                         "rows_per_symbol": {s: int((ds["sym"] == s).sum()) for s in sorted(set(ds["sym"]))},
                                         "missing_by_reason": ds["missing"], "effective": ds["effective"],
                                         "feature_schema_sha256": FEATURE_SCHEMA_SHA256,
                                         "target_schema": SPEC["targets"], "missing_value_policy":
                                             "rows lacking any family or target are excluded for EVERY ablation; no imputation",
                                         "identities": ids})
    par = parity_report(price, kf, ki, ds)
    dump("feature_parity_report.json", par)
    res = run_experiment(ds, parity_ok=par["parity"], identity_ok=identity_ok)
    summary = {"version": VERSION, "evidence_label": EVIDENCE_LABEL, "code_sha": a.code_sha, "spec_sha256": SPEC_SHA256,
               "feature_schema_sha256": FEATURE_SCHEMA_SHA256, "alpha_dataset_sha256": dsha, **ids,
               "identity_checks": checks, "search": SEARCH, "parity": par["parity"],
               "railway_runtime_reachability": "NOT_VERIFIED",
               "common": {"start": int(ds["T"].min()) if len(ds["T"]) else None,
                          "end": int(ds["T"].max()) if len(ds["T"]) else None, "days": res.get("common_days"),
                          "rows": int(len(ds["T"])), "effective": ds["effective"], "missing": ds["missing"]},
               **{k: res.get(k) for k in ("status", "result", "classification_reason", "reason", "primary_selection",
                                          "per_set_selection", "families", "posthoc_best_outer", "estimate", "layout")}}
    if res["status"] == "OK":
        g = {k: v for k, v in res["gate"].items() if not k.startswith("_")}
        summary["gate"] = g
        dump("ablation_matrix.json", {"sets": {k: {"name": SET_NAMES[k], "families": SETS[k]} for k in SETS},
                                      "matched_control": CONTROL, "per_set_outer": res["families"]["per_set_outer"]})
        dump("incremental_uplift.json", res["families"]["matched_uplift"] | {"primary": {k: g.get(k) for k in (
            "uplift_per_slot", "uplift_per_outer_fold", "trade_count_change", "turnover_change_notional_per_risk",
            "cost_change_r", "drawdown_change_r")}})
        dump("cost_stress.json", {"primary": g.get("cost_stress")})
        dump("concentration_report.json", {"primary": g.get("concentration")})
        fr = []
        for si in range(2):
            for sk in PROMOTABLE + DIAGNOSTIC:
                cid = res["per_set_selection"][si].get(sk)
                if cid:
                    o = res["_res"][si][sk][cid]
                    fr.append({"step": si + 1, "set": sk, "cfg": cid, "validation": stats([t["r"] for t in o["val_trades"]]),
                               "outer": stats([t["r"] for t in o["test_trades"]])})
        dump("fold_results.json", fr)
        dump("raw_feature_diagnostics.json", raw_diagnostics(ds, res["_fold"]))
        dump("portfolio_analysis.json", lh.portfolio(res["gate"]["_approved"]) if res["gate"].get("_approved")
             else {"status": "NOT_RUN_NO_APPROVED_TRADES"})
        with gzip.open(out / "candidate_ledger.json.gz", "wt") as fh:
            fh.write(json.dumps(res["ledger"], sort_keys=True, default=str))
        if res["result"] == "CANDIDATE_SUPPORTED":
            b = freeze(res, ds, identities={"training_dataset_sha256": dsha, **ids}, code_sha=a.code_sha,
                       created_at=a.created_at)
            shas = export(b, out / "challenger_bundle")
            v1, v2 = verify(out / "challenger_bundle"), verify(out / "challenger_bundle")
            if not (v1["verified"] and v1 == v2):
                import shutil
                shutil.rmtree(out / "challenger_bundle")
                summary["result"] = "NO_INCREMENTAL_ALPHA"
                summary["gate"]["failures"] = sorted(set(summary["gate"]["failures"] + ["BUNDLE_NOT_DETERMINISTIC"]))
            else:
                summary["frozen"] = {"manifest": {k: v for k, v in b["manifest"].items() if k not in ("probe",)},
                                     "file_sha256": shas, "verify": v1}
    else:
        for n in ("ablation_matrix.json", "incremental_uplift.json", "cost_stress.json", "concentration_report.json",
                  "fold_results.json", "raw_feature_diagnostics.json", "portfolio_analysis.json"):
            dump(n, {"status": res["status"], "reason": res.get("reason")})
        with gzip.open(out / "candidate_ledger.json.gz", "wt") as fh:
            fh.write("[]")
    dump("phase8g_b_summary.json", summary)
    print(json.dumps({k: summary.get(k) for k in ("result", "classification_reason", "status", "spec_sha256",
                                                  "alpha_dataset_sha256", "identity_checks", "common", "parity")},
                     indent=1, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
