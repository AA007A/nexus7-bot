"""LONG_HORIZON_FEATURES_V1: 1h / 4h / 1D decision-time features (Phase 8F).

Single data source: exchange 1h candles. 4h and 1D bars are aggregates of
COMPLETE groups of consecutive 1h bars aligned to UTC (4h: 00/04/..; 1D:
00:00); an incomplete group produces no bar. Decision at time T (open of 1h
bar i) sees 1h bars with close <= T and HTF bars whose close <= T.

Two independent implementations of the same finite-window formulas:
``series`` (vectorized research path) and ``runtime_raw`` (closed-candle
windows a runtime observer would hold); ``parity_report`` compares them.
"""
from __future__ import annotations

import bisect
import hashlib
import json
import math

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view as _swv

VERSION = "LONG_HORIZON_FEATURES_V1"
H1 = 3_600_000
TF_MS = {"h1": H1, "h4": 4 * H1, "d1": 24 * H1}
TFS = ("h1", "h4", "d1")
W = 70                                          # closed bars per timeframe a runtime window needs
RUNTIME_1H_BARS = W * 24 + 48                   # 1h history that yields W complete daily bars
IND_KEYS = ("c", "o_last", "h_last", "l_last", "sma20", "sma50", "std20", "atr14", "donhi20", "donlo20",
            "hi10", "lo10", "atr_rank50", "roc10", "range_ratio", "body", "trend", "range", "structure")
RAW_KEYS = tuple(f"{tf}_{k}" for tf in TFS for k in IND_KEYS)
MODEL_FEATURES = ("trig_z20", "trig_ma_gap_atr", "trig_donchian_pos", "trig_atr_pct", "trig_atr_rank50",
                  "trig_roc10", "trig_range_ratio", "trig_body", "trig_trend", "trig_structure",
                  "h4_trend", "h4_range", "d1_trend", "d1_range", "d1_structure", "d1_dist_atr",
                  "stop_frac", "cost_to_risk")
SCHEMA = {"version": VERSION, "source": "exchange 1h candles; 4h/1D = complete UTC-aligned 1h groups",
          "timeframes": list(TFS), "indicator_keys": list(IND_KEYS), "model_features": list(MODEL_FEATURES),
          "window_bars_per_tf": W, "runtime_1h_bars": RUNTIME_1H_BARS,
          "formulas": {
              "atr14": "mean true range of the last 14 bars", "sma20/sma50": "simple mean of closes",
              "std20": "population std of the last 20 closes",
              "donhi20/donlo20": "max high / min low of the 20 bars BEFORE the last bar",
              "hi10/lo10": "max high / min low of the last 10 bars",
              "atr_rank50": "fraction of atr14 over the 50 bars ending at the previous bar <= that bar's atr14",
              "roc10": "close / close 10 bars earlier - 1", "range_ratio": "(high-low) / previous bar atr14",
              "body": "sign(close-open)",
              "trend": "+1 if close>sma20>sma50 and sma20 > sma20 3 bars earlier; -1 mirror; else 0",
              "range": "1 if |close-sma20|/atr14 < 0.75 and |sma20 - sma20 3 bars earlier| < 0.5*atr14",
              "structure": "+1 if max high and min low of the last 10 bars both exceed those of the "
                           "10 bars before; -1 if both lower; else 0"}}
SCHEMA_SHA256 = hashlib.sha256(json.dumps(SCHEMA, sort_keys=True).encode()).hexdigest()


# ── HTF aggregation (shared by research and runtime) ───────────────────────
def aggregate(c1h, tf: str) -> list[dict]:
    if tf == "h1":
        return list(c1h)
    ms = TF_MS[tf]
    k = ms // H1
    out, group, gstart = [], [], None

    def flush():
        if gstart is not None and len(group) == k and all(int(b["ts"]) == gstart + j * H1 for j, b in enumerate(group)):
            out.append(_merge(group, gstart))
    for b in c1h:
        t = int(b["ts"])
        s = t - t % ms
        if s != gstart:
            flush()
            group, gstart = [], s
        group.append(b)
    flush()
    return out


def _merge(g, ts):
    return {"ts": ts, "o": float(g[0]["o"]), "h": max(float(b["h"]) for b in g), "l": min(float(b["l"]) for b in g),
            "c": float(g[-1]["c"]), "v": sum(float(b.get("v", 0.0)) for b in g)}


# ── vectorized (research) path ─────────────────────────────────────────────
def _roll(x, n, fn):
    out = np.full(len(x), np.nan)
    if len(x) >= n:
        out[n - 1:] = fn(_swv(x, n), axis=1)
    return out


def _shift(x, k):
    out = np.full(len(x), np.nan)
    if 0 < k < len(x):
        out[k:] = x[:len(x) - k]
    elif k == 0:
        out[:] = x
    return out


def ind_series(o, h, lo, c) -> dict:
    n = len(c)
    pc = _shift(c, 1)
    tr = np.fmax(h - lo, np.fmax(np.abs(h - pc), np.abs(lo - pc))) + np.where(np.isnan(pc), np.nan, 0.0)
    atr = _roll(tr, 14, np.mean)
    s20, s50 = _roll(c, 20, np.mean), _roll(c, 50, np.mean)
    s3 = _shift(s20, 3)
    rank = np.full(n, np.nan)
    if n >= 51:
        win = _swv(atr, 50)[:-1]                        # row t = atr[t..t+49] -> bar k = t + 50
        r = np.mean(win <= win[:, -1:], axis=1)
        r[np.isnan(win).any(axis=1)] = np.nan
        rank[50:] = r
    hi10, lo10 = _roll(h, 10, np.max), _roll(lo, 10, np.min)
    phi, plo = _shift(hi10, 10), _shift(lo10, 10)
    with np.errstate(invalid="ignore"):
        trend = np.where((c > s20) & (s20 > s50) & (s20 > s3), 1.0, np.where((c < s20) & (s20 < s50) & (s20 < s3), -1.0, 0.0))
        trend[np.isnan(s50) | np.isnan(s3)] = np.nan
        rng = ((np.abs(c - s20) / atr < 0.75) & (np.abs(s20 - s3) < 0.5 * atr)).astype(float)
        rng[np.isnan(s3) | np.isnan(atr)] = np.nan
        st = np.where((hi10 > phi) & (lo10 > plo), 1.0, np.where((hi10 < phi) & (lo10 < plo), -1.0, 0.0))
        st[np.isnan(phi)] = np.nan
    return {"c": c, "o_last": o, "h_last": h, "l_last": lo, "sma20": s20, "sma50": s50, "std20": _roll(c, 20, np.std),
            "atr14": atr, "donhi20": _shift(_roll(h, 20, np.max), 1), "donlo20": _shift(_roll(lo, 20, np.min), 1),
            "hi10": hi10, "lo10": lo10, "atr_rank50": rank, "roc10": c / _shift(c, 10) - 1.0,
            "range_ratio": (h - lo) / _shift(atr, 1), "body": np.sign(c - o), "trend": trend, "range": rng,
            "structure": st}


def _cols(bars):
    return [np.array([float(b[k]) for b in bars], float) for k in ("o", "h", "l", "c")]


def series(c1h) -> dict:
    """Raw features for every decision index i of the 1h series."""
    ts = np.array([int(b["ts"]) for b in c1h], np.int64)
    out = {"ts": ts}
    for tf in TFS:
        bars = aggregate(c1h, tf)
        if not bars:
            for k in IND_KEYS:
                out[f"{tf}_{k}"] = np.full(len(ts), np.nan)
            continue
        ind = ind_series(*_cols(bars))
        close_t = np.array([int(b["ts"]) for b in bars], np.int64) + TF_MS[tf]
        kk = np.searchsorted(close_t, ts, side="right") - 1
        ok = kk >= 0
        for k, arr in ind.items():
            v = np.full(len(ts), np.nan)
            v[ok] = arr[kk[ok]]
            out[f"{tf}_{k}"] = v
    return out


# ── per-decision (runtime) path ────────────────────────────────────────────
def _nan():
    return float("nan")


def _tr(w, k):
    b, p = w[k], w[k - 1]
    return max(b["h"] - b["l"], abs(b["h"] - p["c"]), abs(b["l"] - p["c"]))


def _atr(w, k):
    if k - 14 < 0:
        return _nan()
    return float(np.mean([_tr(w, j) for j in range(k - 13, k + 1)]))


def _sma(cl, k, n):
    return float(np.mean(cl[k - n + 1:k + 1])) if k - n + 1 >= 0 else _nan()


def ind_last(bars) -> dict:
    """Indicators at the LAST bar of a closed window (runtime path)."""
    w = [{k: float(b[k]) for k in ("o", "h", "l", "c")} for b in bars[-W:]]
    out = {k: _nan() for k in IND_KEYS}
    k = len(w) - 1
    if k < 0:
        return out
    cl = [b["c"] for b in w]
    last = w[k]
    out.update({"c": last["c"], "o_last": last["o"], "h_last": last["h"], "l_last": last["l"],
                "body": float(np.sign(last["c"] - last["o"]))})
    s20, s50 = _sma(cl, k, 20), _sma(cl, k, 50)
    out["sma20"], out["sma50"] = s20, s50
    if k >= 19:
        out["std20"] = float(np.std(cl[k - 19:k + 1]))
    atr = _atr(w, k)
    out["atr14"] = atr
    if k >= 20:
        out["donhi20"] = max(b["h"] for b in w[k - 20:k])
        out["donlo20"] = min(b["l"] for b in w[k - 20:k])
    if k >= 9:
        out["hi10"] = max(b["h"] for b in w[k - 9:k + 1])
        out["lo10"] = min(b["l"] for b in w[k - 9:k + 1])
    if k >= 10:
        out["roc10"] = cl[k] / cl[k - 10] - 1.0
    prev = _atr(w, k - 1)
    if not math.isnan(prev):
        out["range_ratio"] = (last["h"] - last["l"]) / prev
    if k - 50 - 14 >= 0:
        atrs = [_atr(w, j) for j in range(k - 50, k)]
        out["atr_rank50"] = float(np.mean([a <= atrs[-1] for a in atrs]))
    s3 = _sma(cl, k - 3, 20) if k - 3 >= 0 else _nan()
    if not (math.isnan(s50) or math.isnan(s3)):
        c = cl[k]
        out["trend"] = 1.0 if (c > s20 and s20 > s50 and s20 > s3) else -1.0 if (c < s20 and s20 < s50 and s20 < s3) else 0.0
    if not (math.isnan(s3) or math.isnan(atr)):
        out["range"] = 1.0 if (abs(cl[k] - s20) / atr < 0.75 and abs(s20 - s3) < 0.5 * atr) else 0.0
    if k >= 19:
        hi, lo = out["hi10"], out["lo10"]
        phi, plo = max(b["h"] for b in w[k - 19:k - 9]), min(b["l"] for b in w[k - 19:k - 9])
        out["structure"] = 1.0 if (hi > phi and lo > plo) else -1.0 if (hi < phi and lo < plo) else 0.0
    return out


def closed_1h(c1h, decision_ts: int, ts_index=None):
    ts = ts_index if ts_index is not None else [int(b["ts"]) for b in c1h]
    n = bisect.bisect_right(ts, int(decision_ts) - H1)            # bars with ts + 1h <= T
    return c1h[max(0, n - RUNTIME_1H_BARS):n]


def runtime_raw(c1h, decision_ts: int, ts_index=None) -> dict:
    w1 = closed_1h(c1h, decision_ts, ts_index)
    out = {}
    for tf in TFS:
        ind = ind_last(aggregate(w1, tf)) if w1 else {k: _nan() for k in IND_KEYS}
        for k in IND_KEYS:
            out[f"{tf}_{k}"] = ind[k]
    return out


# ── model features ─────────────────────────────────────────────────────────
def model_vector(F, i, side: float, trig: str, stop_frac: float, cost_frac: float) -> list:
    g = (lambda k: float(F[k][i])) if i is not None else (lambda k: float(F[k]))   # noqa: E731
    p = f"{trig}_"
    c, s20, std, atr = g(p + "c"), g(p + "sma20"), g(p + "std20"), g(p + "atr14")
    hi, lo = g(p + "donhi20"), g(p + "donlo20")
    dp = (c - lo) / (hi - lo) if hi > lo else 0.5
    d1a = g("d1_atr14")
    vals = [side * (c - s20) / std if std > 0 else 0.0, side * (s20 - g(p + "sma50")) / atr if atr > 0 else 0.0,
            dp if side > 0 else 1.0 - dp, atr / c if c > 0 else 0.0, g(p + "atr_rank50"), side * g(p + "roc10"),
            g(p + "range_ratio"), side * g(p + "body"), side * g(p + "trend"), side * g(p + "structure"),
            side * g("h4_trend"), g("h4_range"), side * g("d1_trend"), g("d1_range"), side * g("d1_structure"),
            side * (g("d1_c") - g("d1_sma20")) / d1a if d1a > 0 else 0.0,
            float(stop_frac), float(cost_frac) / float(stop_frac) if stop_frac > 0 else 0.0]
    return [0.0 if (v is None or not math.isfinite(v)) else float(np.clip(v, -50.0, 50.0)) for v in vals]


# ── parity ─────────────────────────────────────────────────────────────────
PARITY_TOL = 1e-9


def parity_report(c1h, *, samples: int = 200, runtime=runtime_raw, F=None) -> dict:
    F = F if F is not None else series(c1h)
    n = len(c1h)
    ts_index = [int(b["ts"]) for b in c1h]
    start = RUNTIME_1H_BARS + 1
    idx = sorted(set(int(x) for x in np.linspace(start, n - 1, min(samples, max(0, n - start)))))
    per_tf = {tf: {"values_checked": 0, "max_rel_diff": 0.0, "mismatches": 0} for tf in TFS}
    examples = []
    for i in idx:
        rt = runtime(c1h, int(F["ts"][i]), ts_index)
        for key in RAW_KEYS:
            tf = key[:2]
            a, b = float(F[key][i]), float(rt[key])
            if math.isnan(a) and math.isnan(b):
                continue
            per_tf[tf]["values_checked"] += 1
            bad = math.isnan(a) != math.isnan(b)
            if not bad:
                d = abs(a - b) / max(1.0, abs(a), abs(b))
                per_tf[tf]["max_rel_diff"] = max(per_tf[tf]["max_rel_diff"], d)
                bad = d > PARITY_TOL
            if bad:
                per_tf[tf]["mismatches"] += 1
                if len(examples) < 10:
                    examples.append({"i": i, "key": key, "research": a, "runtime": b})
    ok = bool(idx) and all(v["mismatches"] == 0 and v["values_checked"] > 0 for v in per_tf.values())
    return {"schema": VERSION, "schema_sha256": SCHEMA_SHA256, "decisions": len(idx), "per_timeframe": per_tf,
            "values_checked": sum(v["values_checked"] for v in per_tf.values()),
            "max_rel_diff": max(v["max_rel_diff"] for v in per_tf.values()),
            "mismatches": sum(v["mismatches"] for v in per_tf.values()), "examples": examples, "parity": ok}
