"""ARCH_FEATURES_V1: decision-time market-structure features (Phase 8E).

Two independent implementations of the SAME deterministic formulas:

* ``series(...)``    vectorized over a whole history (research / replay path);
* ``at_decision(...)`` from the closed-candle windows a runtime observer has at
  one decision timestamp (runtime / SHADOW path).

Every indicator uses a FINITE look-back (simple means, rolling extremes, rank
over a fixed window) so the two paths are exactly reproducible from closed
candles; ``parity_report`` checks them against each other. Decision at time T
(open of 15m bar i) sees 15m bars < i and 1h/4h bars whose close time <= T.
Nothing at or after T is ever read.
"""
from __future__ import annotations

import hashlib
import json
import math

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view as _swv

VERSION = "ARCH_FEATURES_V1"
BAR15_MS, BAR1H_MS, BAR4H_MS = 15 * 60_000, 60 * 60_000, 240 * 60_000
W15, W1H, W4H = 120, 50, 24                       # closed bars a runtime window needs

RAW_KEYS = ("c", "o_last", "h_last", "l_last", "sma20", "sma50", "std20", "atr14", "donhi32", "donlo32",
            "hi8", "lo8", "prior_atr_rank96", "roc16", "volz20", "range_ratio", "body",
            "trend1h", "slope1h", "trend4h", "range4h", "dist4h")
MODEL_FEATURES = ("z20", "ma_gap_atr", "donchian_pos", "atr_pct", "atr_rank96", "roc16", "volz20", "range_ratio",
                  "body", "trend1h", "slope1h", "trend4h", "range4h", "dist4h", "stop_frac", "cost_to_risk")
SCHEMA = {"version": VERSION, "raw_keys": list(RAW_KEYS), "model_features": list(MODEL_FEATURES),
          "windows": {"15m": W15, "1h": W1H, "4h": W4H},
          "formulas": {
              "atr14": "mean true range of the last 14 closed bars",
              "sma20/sma50": "simple mean of the last 20/50 closes", "std20": "population std of last 20 closes",
              "donhi32/donlo32": "max high / min low of the 32 closed bars BEFORE the last closed bar",
              "hi8/lo8": "max high / min low of the last 8 closed bars",
              "prior_atr_rank96": "fraction of atr14 over the 96 bars ending at the previous bar <= that bar's atr14",
              "roc16": "close / close 16 bars earlier - 1",
              "volz20": "(last volume - mean prior 20) / std prior 20 (0 if std 0)",
              "range_ratio": "(last high - last low) / previous bar atr14", "body": "sign(last close - last open)",
              "trend1h": "+1 if sma20>sma50 and close>sma20; -1 mirror; else 0 (1h)",
              "slope1h": "(sma20 - sma20 3 bars earlier) / close (1h)",
              "trend4h": "+1 if close>sma20 and sma20 rising over 3 bars; -1 mirror; else 0 (4h)",
              "range4h": "|close-sma20|/atr14 < 0.75 and |sma20 change over 3 bars| < 0.5*atr14 (4h)",
              "dist4h": "(close - sma20) / atr14 (4h)"}}
SCHEMA_SHA256 = hashlib.sha256(json.dumps(SCHEMA, sort_keys=True).encode()).hexdigest()


# ── vectorized (research) path ─────────────────────────────────────────────
def _roll(x, n, fn):
    out = np.full(len(x), np.nan)
    if len(x) >= n:
        out[n - 1:] = fn(_swv(x, n), axis=1)
    return out


def _shift(x, k):
    out = np.full(len(x), np.nan)
    if k < len(x):
        out[k:] = x[:len(x) - k]
    return out


def _tr(h, lo, c):
    pc = _shift(c, 1)
    return np.fmax(h - lo, np.fmax(np.abs(h - pc), np.abs(lo - pc))) + np.where(np.isnan(pc), np.nan, 0.0)


def _ind15(o, h, lo, c, v):
    atr = _roll(_tr(h, lo, c), 14, np.mean)
    out = {"c": c, "o_last": o, "h_last": h, "l_last": lo,
           "sma20": _roll(c, 20, np.mean), "sma50": _roll(c, 50, np.mean), "std20": _roll(c, 20, np.std),
           "atr14": atr, "donhi32": _shift(_roll(h, 32, np.max), 1), "donlo32": _shift(_roll(lo, 32, np.min), 1),
           "hi8": _roll(h, 8, np.max), "lo8": _roll(lo, 8, np.min),
           "roc16": c / _shift(c, 16) - 1.0, "body": np.sign(c - o)}
    prev_atr = _shift(atr, 1)
    rank = np.full(len(c), np.nan)
    if len(c) >= 97:
        win = _swv(atr, 96)[:-1]                       # row t = atr[t .. t+95] -> decision bar k = t + 96
        r = np.mean(win <= win[:, -1:], axis=1)
        r[np.isnan(win).any(axis=1)] = np.nan
        rank[96:] = r
    out["prior_atr_rank96"] = rank
    pv_m = _shift(_roll(v, 20, np.mean), 1)
    pv_s = _shift(_roll(v, 20, np.std), 1)
    out["volz20"] = np.where(pv_s > 0, (v - pv_m) / np.where(pv_s > 0, pv_s, 1.0), np.where(np.isnan(pv_s), np.nan, 0.0))
    out["range_ratio"] = (h - lo) / prev_atr
    return out


def _ind1h(o, h, lo, c):
    s20, s50 = _roll(c, 20, np.mean), _roll(c, 50, np.mean)
    t = np.where((s20 > s50) & (c > s20), 1.0, np.where((s20 < s50) & (c < s20), -1.0, 0.0))
    t[np.isnan(s50)] = np.nan
    return {"trend1h": t, "slope1h": (s20 - _shift(s20, 3)) / c}


def _ind4h(o, h, lo, c):
    s20, atr = _roll(c, 20, np.mean), _roll(_tr(h, lo, c), 14, np.mean)
    s3 = _shift(s20, 3)
    t = np.where((c > s20) & (s20 > s3), 1.0, np.where((c < s20) & (s20 < s3), -1.0, 0.0))
    rng = ((np.abs(c - s20) / atr < 0.75) & (np.abs(s20 - s3) < 0.5 * atr)).astype(float)
    bad = np.isnan(s3) | np.isnan(atr)
    t[bad], rng[bad] = np.nan, np.nan
    return {"trend4h": t, "range4h": rng, "dist4h": (c - s20) / atr}


def _cols(candles, keys=("o", "h", "l", "c", "v")):
    return [np.array([float(b[k]) for b in candles], float) for k in keys]


def series(c15, c1h, c4h) -> dict:
    """Raw features for EVERY decision index i of the 15m series (bars < i)."""
    n = len(c15)
    ts15 = np.array([int(b["ts"]) for b in c15], np.int64)
    i15 = _ind15(*_cols(c15))
    i1 = _ind1h(*_cols(c1h, ("o", "h", "l", "c")))
    i4 = _ind4h(*_cols(c4h, ("o", "h", "l", "c")))
    k1 = np.searchsorted(np.array([int(b["ts"]) for b in c1h], np.int64) + BAR1H_MS, ts15, side="right") - 1
    k4 = np.searchsorted(np.array([int(b["ts"]) for b in c4h], np.int64) + BAR4H_MS, ts15, side="right") - 1
    out = {"ts": ts15}
    for key, arr in i15.items():
        out[key] = _shift(arr, 1)                         # decision i uses closed bar i-1
    for src, k in ((i1, k1), (i4, k4)):
        for key, arr in src.items():
            v = np.full(n, np.nan)
            ok = k >= 0
            v[ok] = arr[k[ok]]
            out[key] = v
    return out


# ── per-decision (runtime) path ────────────────────────────────────────────
def closed_window(candles, decision_ts: int, bar_ms: int, n: int):
    """The last n candles whose close time <= decision_ts (runtime window)."""
    out = [b for b in candles if int(b["ts"]) + bar_ms <= int(decision_ts)]
    return out[-n:]


def _nan():
    return float("nan")


def _last_tr(w, k):
    b, p = w[k], w[k - 1]
    return max(b["h"] - b["l"], abs(b["h"] - p["c"]), abs(b["l"] - p["c"]))


def _atr_at(w, k, n=14):
    if k - n < 0:
        return _nan()
    return float(np.mean([_last_tr(w, j) for j in range(k - n + 1, k + 1)]))


def at_decision(w15, w1h, w4h) -> dict:
    """Raw features from runtime windows (closed candles only)."""
    w15 = [{k: float(b[k]) for k in ("o", "h", "l", "c", "v")} for b in w15]
    w1h = [{k: float(b[k]) for k in ("o", "h", "l", "c")} for b in w1h]
    w4h = [{k: float(b[k]) for k in ("o", "h", "l", "c")} for b in w4h]
    out = {k: _nan() for k in RAW_KEYS}
    k = len(w15) - 1
    if k >= 0:
        last = w15[k]
        closes = [b["c"] for b in w15]
        out.update({"c": last["c"], "o_last": last["o"], "h_last": last["h"], "l_last": last["l"],
                    "body": float(np.sign(last["c"] - last["o"]))})
        if k >= 19:
            out["sma20"] = float(np.mean(closes[k - 19:]))
            out["std20"] = float(np.std(closes[k - 19:]))
        if k >= 49:
            out["sma50"] = float(np.mean(closes[k - 49:]))
        atr = _atr_at(w15, k)
        out["atr14"] = atr
        if k >= 32:
            out["donhi32"] = max(b["h"] for b in w15[k - 32:k])
            out["donlo32"] = min(b["l"] for b in w15[k - 32:k])
        if k >= 7:
            out["hi8"] = max(b["h"] for b in w15[k - 7:])
            out["lo8"] = min(b["l"] for b in w15[k - 7:])
        if k >= 16:
            out["roc16"] = closes[k] / closes[k - 16] - 1.0
        if k >= 20:
            pv = np.array([b["v"] for b in w15[k - 20:k]])
            m, s = float(np.mean(pv)), float(np.std(pv))
            out["volz20"] = (w15[k]["v"] - m) / s if s > 0 else 0.0
        prev = _atr_at(w15, k - 1)
        if not math.isnan(prev):
            out["range_ratio"] = (last["h"] - last["l"]) / prev
        if k - 1 - 95 - 14 >= 0:
            atrs = [_atr_at(w15, j) for j in range(k - 96, k)]
            out["prior_atr_rank96"] = float(np.mean([a <= atrs[-1] for a in atrs]))
    k1 = len(w1h) - 1
    c1 = [b["c"] for b in w1h]
    if k1 >= 22:
        out["slope1h"] = (float(np.mean(c1[k1 - 19:])) - float(np.mean(c1[k1 - 22:k1 - 2]))) / c1[k1]
    if k1 >= 49:
        s20, s50 = float(np.mean(c1[k1 - 19:])), float(np.mean(c1[k1 - 49:]))
        out["trend1h"] = 1.0 if (s20 > s50 and c1[k1] > s20) else -1.0 if (s20 < s50 and c1[k1] < s20) else 0.0
    k4 = len(w4h) - 1
    c4 = [b["c"] for b in w4h]
    atr4 = _atr_at(w4h, k4) if k4 >= 14 else _nan()
    if k4 >= 19 and not math.isnan(atr4):
        out["dist4h"] = (c4[k4] - float(np.mean(c4[k4 - 19:]))) / atr4
    if k4 >= 22 and not math.isnan(atr4):
        s20, s3 = float(np.mean(c4[k4 - 19:])), float(np.mean(c4[k4 - 22:k4 - 2]))
        out["trend4h"] = 1.0 if (c4[k4] > s20 and s20 > s3) else -1.0 if (c4[k4] < s20 and s20 < s3) else 0.0
        out["range4h"] = 1.0 if (abs(c4[k4] - s20) / atr4 < 0.75 and abs(s20 - s3) < 0.5 * atr4) else 0.0
    return out


def runtime_raw(c15, c1h, c4h, decision_ts: int) -> dict:
    return at_decision(closed_window(c15, decision_ts, BAR15_MS, W15), closed_window(c1h, decision_ts, BAR1H_MS, W1H),
                       closed_window(c4h, decision_ts, BAR4H_MS, W4H))


# ── model features (direction-signed, scale-free) ──────────────────────────
def _g(F, k, i):
    v = F[k] if i is None else F[k][i]
    return float(v)


def model_vector(F, i, side: float, stop_frac: float, cost_frac: float) -> list:
    """MODEL_FEATURES for decision i (F = series dict) or a runtime dict (i=None)."""
    g = lambda k: _g(F, k, i)                          # noqa: E731
    c, s20, std, atr = g("c"), g("sma20"), g("std20"), g("atr14")
    hi, lo = g("donhi32"), g("donlo32")
    dp = (c - lo) / (hi - lo) if hi > lo else 0.5
    vals = [side * (c - s20) / std if std > 0 else 0.0,
            side * (s20 - g("sma50")) / atr if atr > 0 else 0.0,
            dp if side > 0 else 1.0 - dp,
            atr / c if c > 0 else 0.0, g("prior_atr_rank96"), side * g("roc16"), g("volz20"), g("range_ratio"),
            side * g("body"), side * g("trend1h"), side * g("slope1h"), side * g("trend4h"), g("range4h"),
            side * g("dist4h"), float(stop_frac), float(cost_frac) / float(stop_frac) if stop_frac > 0 else 0.0]
    return [0.0 if (v is None or not math.isfinite(v)) else float(np.clip(v, -50.0, 50.0)) for v in vals]


# ── parity ─────────────────────────────────────────────────────────────────
PARITY_TOL = 1e-9


def parity_report(c15, c1h, c4h, *, samples: int = 200, runtime=runtime_raw) -> dict:
    """Compare the research series against the runtime path at sampled decisions."""
    F = series(c15, c1h, c4h)
    n = len(c15)
    idx = sorted(set(int(x) for x in np.linspace(W15 + 1, n - 1, min(samples, max(0, n - W15 - 1)))))
    worst, mismatches, checked = 0.0, [], 0
    for i in idx:
        rt = runtime(c15, c1h, c4h, int(F["ts"][i]))
        for k in RAW_KEYS:
            a, b = float(F[k][i]), float(rt[k])
            if math.isnan(a) and math.isnan(b):
                continue
            checked += 1
            if math.isnan(a) != math.isnan(b):
                mismatches.append({"i": i, "key": k, "research": a, "runtime": b})
                continue
            d = abs(a - b) / max(1.0, abs(a), abs(b))
            worst = max(worst, d)
            if d > PARITY_TOL:
                mismatches.append({"i": i, "key": k, "research": a, "runtime": b})
    return {"schema": VERSION, "schema_sha256": SCHEMA_SHA256, "decisions": len(idx), "values_checked": checked,
            "max_rel_diff": worst, "mismatches": len(mismatches), "examples": mismatches[:10],
            "parity": bool(idx) and not mismatches and checked > 0}
