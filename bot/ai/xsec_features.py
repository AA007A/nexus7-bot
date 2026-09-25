"""CROSS_SECTIONAL_FEATURES_V1 (Phase 8G-A): cross-sectional / market-state /
volume-volatility features derived ONLY from the exact 12-symbol public 1h
price dataset (no new external data). DATA AUDIT ONLY - no outcomes used.

Point-in-time rule: at decision time T (a UTC hour), a symbol contributes
only if its 1h bar that closed exactly at T exists (no stale carry-forward);
every look-back requires the exact hourly bars (a missing bar makes the
value missing, never zero). Two independent implementations - ``matrix``
(vectorized research path) and ``runtime_at`` (per-decision from closed
windows) - are compared by ``parity_report``.
"""
from __future__ import annotations

import bisect
import hashlib
import json
import math

import numpy as np

VERSION = "CROSS_SECTIONAL_FEATURES_V1"
H1 = 3_600_000
LOOKBACK_H = 720 + 1                               # longest look-back (30d volume baseline) + 1
PER_SYMBOL = ("ret_1h", "ret_24h", "ret_72h", "ret_168h", "rank_ret_24h", "rank_ret_168h", "rvol_24h",
              "rank_rvol_24h", "rel_btc_24h", "rel_eth_24h", "rel_mkt_24h", "beta_btc_168h", "corr_btc_168h",
              "resid_24h", "leader", "laggard", "rvol_term_24_168", "vol_of_vol_7d", "volume_shock_24h",
              "volume_z_1h", "above_sma480")
MARKET = ("n_available", "breadth_sma480", "dispersion_24h", "mkt_ret_24h", "btc_trend_sma480", "btc_rvol_24h",
          "eth_trend_sma480", "alt_mom_168h")
SCHEMA = {"version": VERSION, "per_symbol": list(PER_SYMBOL), "market": list(MARKET),
          "point_in_time": "values at T use only 1h bars with close <= T; symbol present only if its bar closing at T exists",
          "formulas": {
              "ret_k": "log(close_T / close_{T-k h}), both exact bars required",
              "rank_*": "cross-sectional percentile (average ties) among symbols with the value, in [0,1]",
              "rvol_24h": "population std of the last 24 hourly log returns",
              "rel_btc_24h / rel_eth_24h / rel_mkt_24h": "ret_24h minus BTC / ETH / equal-weight available mean",
              "beta_btc_168h / corr_btc_168h": "OLS beta / Pearson corr of the last 168 hourly log returns vs BTC",
              "resid_24h": "ret_24h - beta_btc_168h * ret_24h(BTC)",
              "leader / laggard": "rank_ret_168h >= 2/3 / <= 1/3 (1.0 / 0.0)",
              "rvol_term_24_168": "rvol_24h / std of the last 168 hourly log returns",
              "vol_of_vol_7d": "std of the 7 non-overlapping 24h rvols ending at T",
              "volume_shock_24h": "sum volume last 24h / (sum volume last 720h / 30)",
              "volume_z_1h": "(volume of last bar - mean of the prior 168) / std of the prior 168",
              "above_sma480": "1 if close_T > mean of the last 480 closes else 0",
              "market": "breadth = mean(above_sma480); dispersion = std(ret_24h); mkt_ret = mean(ret_24h); "
                        "btc/eth trend = above_sma480 of BTC/ETH; alt_mom = mean ret_168h excluding BTC and ETH"}}
SCHEMA_SHA256 = hashlib.sha256(json.dumps(SCHEMA, sort_keys=True).encode()).hexdigest()


def _rank(v: np.ndarray) -> np.ndarray:
    out = np.full(len(v), np.nan)
    ok = np.isfinite(v)
    x = v[ok]
    if len(x) == 0:
        return out
    if len(x) == 1:
        out[ok] = 0.5
        return out
    order = np.argsort(x, kind="mergesort")
    ranks = np.empty(len(x))
    i = 0
    xs = x[order]
    while i < len(x):
        j = i
        while j + 1 < len(x) and xs[j + 1] == xs[i]:
            j += 1
        ranks[order[i:j + 1]] = (i + j) / 2.0
        i = j + 1
    out[ok] = ranks / (len(x) - 1)
    return out


def _std(x):
    return float(np.std(x)) if len(x) else float("nan")


# ── per-symbol history metrics from a closed window ending at T ────────────
def _symbol_block(closes: np.ndarray, vols: np.ndarray) -> dict:
    """closes/vols: exactly the LOOKBACK_H hourly values ending at T (nan where missing)."""
    c, v = closes, vols
    n = len(c)
    last = n - 1
    lr = np.diff(np.log(c))                     # lr[k] = log return into bar k+1
    out = {}
    for k in (1, 24, 72, 168):
        out[f"ret_{k}h"] = float(np.log(c[last] / c[last - k])) if np.isfinite(c[last]) and np.isfinite(c[last - k]) else np.nan
    r24, r168 = lr[-24:], lr[-168:]
    out["rvol_24h"] = _std(r24) if np.isfinite(r24).all() else np.nan
    s168 = _std(r168) if np.isfinite(r168).all() else np.nan
    out["rvol_term_24_168"] = out["rvol_24h"] / s168 if np.isfinite(out["rvol_24h"]) and s168 and s168 > 0 else np.nan
    chunks = [lr[len(lr) - 24 * (q + 1):len(lr) - 24 * q] for q in range(7)]
    out["vol_of_vol_7d"] = (_std(np.array([_std(ch) for ch in chunks]))
                            if all(np.isfinite(ch).all() for ch in chunks) else np.nan)
    v24, v720 = v[-24:], v[-720:]
    base = np.sum(v720) / 30.0 if np.isfinite(v720).all() else np.nan
    out["volume_shock_24h"] = float(np.sum(v24) / base) if np.isfinite(v24).all() and base and base > 0 else np.nan
    prior = v[-169:-1]
    if np.isfinite(prior).all() and np.isfinite(v[last]):
        sd = _std(prior)
        out["volume_z_1h"] = float((v[last] - np.mean(prior)) / sd) if sd > 0 else 0.0
    else:
        out["volume_z_1h"] = np.nan
    c480 = c[-480:]
    out["above_sma480"] = (1.0 if c[last] > np.mean(c480) else 0.0) if np.isfinite(c480).all() else np.nan
    out["_r168"] = r168
    return out


def _cross(blocks: dict, symbols: list) -> tuple[dict, dict]:
    """Cross-sectional step given per-symbol blocks at one T."""
    per = {s: dict(blocks[s]) for s in symbols}
    for key, src in (("rank_ret_24h", "ret_24h"), ("rank_ret_168h", "ret_168h"), ("rank_rvol_24h", "rvol_24h")):
        vals = np.array([per[s][src] for s in symbols], float)
        rk = _rank(vals)
        for s, r in zip(symbols, rk):
            per[s][key] = float(r)
    r24 = np.array([per[s]["ret_24h"] for s in symbols], float)
    ok = np.isfinite(r24)
    mkt = float(np.mean(r24[ok])) if ok.any() else np.nan
    btc = per.get("BTCUSDT", {}).get("ret_24h", np.nan)
    eth = per.get("ETHUSDT", {}).get("ret_24h", np.nan)
    rb = per.get("BTCUSDT", {}).get("_r168")
    for s in symbols:
        p = per[s]
        p["rel_btc_24h"] = p["ret_24h"] - btc
        p["rel_eth_24h"] = p["ret_24h"] - eth
        p["rel_mkt_24h"] = p["ret_24h"] - mkt
        rs = p["_r168"]
        if rb is not None and np.isfinite(rs).all() and np.isfinite(rb).all():
            vb = float(np.var(rb))
            cov = float(np.mean((rs - rs.mean()) * (rb - rb.mean())))
            p["beta_btc_168h"] = cov / vb if vb > 0 else np.nan
            sd = float(np.std(rs)) * math.sqrt(vb)
            p["corr_btc_168h"] = cov / sd if sd > 0 else np.nan
        else:
            p["beta_btc_168h"] = p["corr_btc_168h"] = np.nan
        p["resid_24h"] = p["ret_24h"] - p["beta_btc_168h"] * btc
        rk = p["rank_ret_168h"]
        p["leader"] = (1.0 if rk >= 2 / 3 else 0.0) if np.isfinite(rk) else np.nan
        p["laggard"] = (1.0 if rk <= 1 / 3 else 0.0) if np.isfinite(rk) else np.nan
    above = np.array([per[s]["above_sma480"] for s in symbols], float)
    r168 = np.array([per[s]["ret_168h"] for s in symbols if s not in ("BTCUSDT", "ETHUSDT")], float)
    mk = {"n_available": float(ok.sum()),
          "breadth_sma480": float(np.nanmean(above)) if np.isfinite(above).any() else np.nan,
          "dispersion_24h": float(np.std(r24[ok])) if ok.sum() >= 2 else np.nan, "mkt_ret_24h": mkt,
          "btc_trend_sma480": per.get("BTCUSDT", {}).get("above_sma480", np.nan),
          "btc_rvol_24h": per.get("BTCUSDT", {}).get("rvol_24h", np.nan),
          "eth_trend_sma480": per.get("ETHUSDT", {}).get("above_sma480", np.nan),
          "alt_mom_168h": float(np.mean(r168[np.isfinite(r168)])) if np.isfinite(r168).any() else np.nan}
    for s in symbols:
        per[s].pop("_r168", None)
    return per, mk


# ── research path: aligned matrix over a common hourly grid ────────────────
def grid(data: dict) -> np.ndarray:
    t0 = min(int(v[0]["ts"]) for v in data.values() if v)
    t1 = max(int(v[-1]["ts"]) for v in data.values() if v) + H1
    return np.arange(t0 - t0 % H1, t1 + H1, H1, dtype=np.int64)       # decision times T (bar close times)


def aligned(data: dict, T: np.ndarray) -> dict:
    """close/volume of the bar CLOSING at each T (nan if that exact bar is missing)."""
    out = {}
    for s, bars in data.items():
        c, v = np.full(len(T), np.nan), np.full(len(T), np.nan)
        pos = {int(b["ts"]) + H1: i for i, b in enumerate(bars)}
        for k, t in enumerate(T):
            i = pos.get(int(t))
            if i is not None:
                c[k], v[k] = float(bars[i]["c"]), float(bars[i]["v"])
        out[s] = (c, v)
    return out


def matrix(data: dict, T_eval=None) -> dict:
    """Features at every T in T_eval (default: the full grid)."""
    symbols = sorted(data)
    T = grid(data)
    al = aligned(data, T)
    idx = {int(t): k for k, t in enumerate(T)}
    T_eval = T if T_eval is None else np.asarray(T_eval, np.int64)
    res = {"T": T_eval, "per_symbol": {s: {k: np.full(len(T_eval), np.nan) for k in PER_SYMBOL} for s in symbols},
           "market": {k: np.full(len(T_eval), np.nan) for k in MARKET}}
    for q, t in enumerate(T_eval):
        k = idx.get(int(t))
        if k is None or k < LOOKBACK_H - 1:
            continue
        blocks, present = {}, []
        for s in symbols:
            c, v = al[s]
            if not np.isfinite(c[k]):
                continue
            blocks[s] = _symbol_block(c[k - LOOKBACK_H + 1:k + 1], v[k - LOOKBACK_H + 1:k + 1])
            present.append(s)
        if not present:
            continue
        per, mk = _cross(blocks, present)
        for s in present:
            for f in PER_SYMBOL:
                res["per_symbol"][s][f][q] = per[s][f]
        for f in MARKET:
            res["market"][f][q] = mk[f]
    return res


# ── runtime path: per-decision from closed windows ─────────────────────────
def runtime_at(data: dict, T: int, ts_index: dict | None = None) -> tuple[dict, dict]:
    symbols = sorted(data)
    blocks, present = {}, []
    for s in symbols:
        bars = data[s]
        ts = ts_index[s] if ts_index else [int(b["ts"]) for b in bars]
        n = bisect.bisect_right(ts, int(T) - H1)                 # bars with ts + 1h <= T
        window = bars[max(0, n - LOOKBACK_H - 5):n]
        if not window or int(window[-1]["ts"]) + H1 != int(T):
            continue                                              # no bar closing exactly at T
        byts = {int(b["ts"]): b for b in window}
        c = np.full(LOOKBACK_H, np.nan)
        v = np.full(LOOKBACK_H, np.nan)
        for j in range(LOOKBACK_H):
            b = byts.get(int(T) - H1 * (LOOKBACK_H - j))
            if b is not None:
                c[j], v[j] = float(b["c"]), float(b["v"])
        blocks[s] = _symbol_block(c, v)
        present.append(s)
    if not present:
        return {}, {k: np.nan for k in MARKET}
    return _cross(blocks, present)


def parity_report(data: dict, *, samples: int = 120, runtime=runtime_at) -> dict:
    T = grid(data)
    cand = T[LOOKBACK_H + 24:]
    pick = cand[np.linspace(0, len(cand) - 1, min(samples, len(cand))).astype(int)] if len(cand) else cand
    M = matrix(data, pick)
    ts_index = {s: [int(b["ts"]) for b in v] for s, v in data.items()}
    checked = mism = 0
    worst = 0.0
    examples = []
    for q, t in enumerate(pick):
        per, mk = runtime(data, int(t), ts_index)
        pairs = [(f"mkt:{f}", M["market"][f][q], mk.get(f, np.nan)) for f in MARKET]
        for s in M["per_symbol"]:
            for f in PER_SYMBOL:
                pairs.append((f"{s}:{f}", M["per_symbol"][s][f][q], per.get(s, {}).get(f, np.nan)))
        for key, a, b in pairs:
            a, b = float(a), float(b)
            if math.isnan(a) and math.isnan(b):
                continue
            checked += 1
            bad = math.isnan(a) != math.isnan(b)
            if not bad:
                d = abs(a - b) / max(1.0, abs(a), abs(b))
                worst = max(worst, d)
                bad = d > 1e-9
            if bad:
                mism += 1
                if len(examples) < 10:
                    examples.append({"T": int(t), "key": key, "research": a, "runtime": b})
    return {"schema": VERSION, "schema_sha256": SCHEMA_SHA256, "decisions": int(len(pick)), "values_checked": checked,
            "max_rel_diff": worst, "mismatches": mism, "examples": examples, "parity": checked > 0 and mism == 0}


def quality(M: dict) -> dict:
    """Descriptive data-quality only (no outcomes)."""
    out = {"decisions": int(len(M["T"])), "per_feature": {}}
    for f in PER_SYMBOL:
        allv = np.concatenate([M["per_symbol"][s][f] for s in M["per_symbol"]])
        fin = allv[np.isfinite(allv)]
        out["per_feature"][f] = {"available_fraction": float(len(fin) / len(allv)) if len(allv) else 0.0,
                                 **({"p01": float(np.quantile(fin, .01)), "p50": float(np.median(fin)),
                                     "p99": float(np.quantile(fin, .99)), "min": float(fin.min()),
                                     "max": float(fin.max())} if len(fin) else {})}
    for f in MARKET:
        v = M["market"][f]
        fin = v[np.isfinite(v)]
        out["per_feature"][f"mkt:{f}"] = {"available_fraction": float(len(fin) / len(v)) if len(v) else 0.0,
                                          **({"p50": float(np.median(fin)), "min": float(fin.min()),
                                              "max": float(fin.max())} if len(fin) else {})}
    return out


def features_sha256(M: dict) -> str:
    h = hashlib.sha256()
    h.update(np.asarray(M["T"], np.int64).tobytes())
    for s in sorted(M["per_symbol"]):
        for f in PER_SYMBOL:
            h.update(np.round(np.nan_to_num(M["per_symbol"][s][f], nan=-9e9), 10).tobytes())
    for f in MARKET:
        h.update(np.round(np.nan_to_num(M["market"][f], nan=-9e9), 10).tobytes())
    return h.hexdigest()
