"""EXOGENOUS_FEATURES_V1 (Phase 8G-B): KuCoin settled funding + perp/index-close basis.

Definitions (exact, used in every artifact):
  funding_rate_settled - KuCoin /api/v1/contract/funding-rates ``fundingRate`` at its
                         settlement ``timepoint``; observable at timepoint.
  perp_index_close_basis = perp_close / index_close - 1 for the SAME completed 1h bar
                         (perp XBTUSDTM-style kline, index .KXBTUSDT-style kline).
  This basis is NOT the mark/index premium, NOT the Binance premium index and NOT an
  annualized futures basis.

Point-in-time: at decision T only settlements with timepoint <= T and only 1h bars
with close time <= T (bar ts <= T - 1h) are used. Missing stays missing (NaN, with
explicit availability), stale plateaus are kept and flagged, irregular settlement
intervals are handled by settlement index (not an assumed 8h grid).

Two independent implementations: vectorized ``funding_series``/``basis_series``
(research) and ``funding_at``/``basis_at`` (runtime, plain Python).
"""
from __future__ import annotations

import bisect
import hashlib
import json
import math

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view as _swv

VERSION = "EXOGENOUS_FEATURES_V1"
H1 = 3_600_000
FUNDING_MAX_AGE_H = 16.0                 # older last settlement => feed gap => missing
F_KEYS = ("f_last", "f_sign", "f_abs", "f_mean9", "f_z21", "f_pct63", "f_change", "f_persist", "f_stale", "f_age_h",
          "f_div")
F_DIRECTIONAL = ("f_last", "f_sign", "f_mean9", "f_z21", "f_pct63", "f_change", "f_persist")
B_KEYS = ("b_now", "b_chg1", "b_chg24", "b_mean24", "b_z168", "b_pct168", "b_slope24", "b_sign", "b_extreme", "b_div")
B_DIRECTIONAL = ("b_now", "b_chg1", "b_chg24", "b_mean24", "b_z168", "b_pct168", "b_slope24", "b_sign")
SCHEMA = {"version": VERSION,
          "definitions": {"funding_rate_settled": "KuCoin fundingRate at settlement timepoint (observable at timepoint)",
                          "perp_index_close_basis": "perp_close / index_close - 1 for the same completed 1h bar; NOT "
                                                    "mark/index premium, NOT Binance premium index, NOT annualized basis"},
          "funding": {"f_last": "rate of the last settlement with timepoint <= T",
                      "f_sign": "sign(f_last)", "f_abs": "|f_last|", "f_mean9": "mean of the last 9 settlements",
                      "f_z21": "(f_last - mean21) / std21 over the last 21 settlements (0 if std == 0: plateau)",
                      "f_pct63": "fraction of the last 63 settlements <= f_last, minus 0.5",
                      "f_change": "f_last - previous settlement rate",
                      "f_persist": "sign(f_last) * number of consecutive same-sign settlements ending at the last "
                                   "(capped 21; a zero rate has persistence 0)",
                      "f_stale": "1 if the last 3 settlement rates are identical (plateau) else 0",
                      "f_age_h": "hours since the last settlement", "f_div": "1 if sign(ret_24h) == -sign(f_mean9) != 0",
                      "availability": f">= 63 settlements and f_age_h <= {FUNDING_MAX_AGE_H}"},
          "basis": {"b_now": "basis of the bar closing at T", "b_chg1": "b_now - basis 1h earlier",
                    "b_chg24": "b_now - basis 24h earlier", "b_mean24": "mean of the last 24 hourly bases",
                    "b_z168": "(b_now - mean168)/std168 (0 if std == 0)", "b_pct168": "fraction of last 168 <= b_now, minus 0.5",
                    "b_slope24": "OLS slope per hour of the last 24 bases", "b_sign": "sign(b_now)",
                    "b_extreme": "1 if |b_z168| > 2", "b_div": "1 if sign(ret_24h) == -sign(b_mean24) != 0",
                    "availability": "all 168 hourly bases present (matched perp AND index completed bars)"},
          "directional_by_side": {"funding": list(F_DIRECTIONAL), "basis": list(B_DIRECTIONAL)}}
SCHEMA_SHA256 = hashlib.sha256(json.dumps(SCHEMA, sort_keys=True).encode()).hexdigest()


def _sgn(x):
    return 0.0 if x == 0 else (1.0 if x > 0 else -1.0)


# ── funding: runtime ───────────────────────────────────────────────────────
def funding_at(settlements, T: int, ret_24h: float) -> dict:
    """settlements: list of (timepoint_ms, rate) sorted; only those <= T are used."""
    n = bisect.bisect_right([s[0] for s in settlements], int(T))
    vis = settlements[:n]
    out = {k: float("nan") for k in F_KEYS}
    if n < 63:
        return out
    age = (int(T) - int(vis[-1][0])) / H1
    if age > FUNDING_MAX_AGE_H or any(r is None or not math.isfinite(float(r)) for _, r in vis[-63:]):
        return out
    r = [float(x[1]) for x in vis[-63:]]
    last = r[-1]
    w21 = r[-21:]
    m21 = sum(w21) / 21
    sd = math.sqrt(sum((x - m21) ** 2 for x in w21) / 21)
    pers = 0
    if last != 0:
        for x in reversed(r):
            if _sgn(x) == _sgn(last):
                pers += 1
            else:
                break
    m9 = sum(r[-9:]) / 9
    out.update({"f_last": last, "f_sign": _sgn(last), "f_abs": abs(last), "f_mean9": m9,
                "f_z21": (last - m21) / sd if sd > 0 else 0.0, "f_pct63": sum(1 for x in r if x <= last) / 63 - 0.5,
                "f_change": last - r[-2], "f_persist": _sgn(last) * min(pers, 21),
                "f_stale": 1.0 if r[-1] == r[-2] == r[-3] else 0.0, "f_age_h": age,
                "f_div": 1.0 if (math.isfinite(ret_24h) and _sgn(ret_24h) != 0 and _sgn(ret_24h) == -_sgn(m9)) else 0.0})
    if not math.isfinite(ret_24h):
        out["f_div"] = float("nan")
    return out


# ── funding: research (vectorized over settlement index, mapped to T) ──────
def funding_series(ts: np.ndarray, rates: np.ndarray, T: np.ndarray, ret_24h: np.ndarray) -> dict:
    ts, r = np.asarray(ts, np.int64), np.asarray(rates, float)
    n = len(r)
    per = {k: np.full(n, np.nan) for k in F_KEYS if k not in ("f_age_h", "f_div")}
    if n >= 63:
        w63 = _swv(r, 63)                                        # row t -> settlement k = t + 62
        w21 = w63[:, -21:]
        m21 = w21.mean(axis=1)
        sd = w21.std(axis=1)
        last = w63[:, -1]
        k = np.arange(62, n)
        per["f_last"][k] = last
        per["f_sign"][k] = np.sign(last)
        per["f_abs"][k] = np.abs(last)
        per["f_mean9"][k] = w63[:, -9:].mean(axis=1)
        per["f_z21"][k] = np.where(sd > 0, (last - m21) / np.where(sd > 0, sd, 1.0), 0.0)
        per["f_pct63"][k] = (w63 <= last[:, None]).sum(axis=1) / 63 - 0.5
        per["f_change"][k] = last - w63[:, -2]
        s = np.sign(r)
        run = np.zeros(n)
        for i in range(n):                                     # consecutive same-sign run (by settlement index)
            run[i] = 0 if s[i] == 0 else (run[i - 1] + 1 if i > 0 and s[i - 1] == s[i] else 1)
        per["f_persist"][k] = s[k] * np.minimum(run[k], 21)
        per["f_stale"][k] = ((w63[:, -1] == w63[:, -2]) & (w63[:, -2] == w63[:, -3])).astype(float)
        bad = ~np.isfinite(w63).all(axis=1)
        for key in per:
            per[key][k[bad]] = np.nan
    idx = np.searchsorted(ts, np.asarray(T, np.int64), side="right") - 1
    out = {}
    ok = idx >= 0
    age = np.full(len(T), np.nan)
    age[ok] = (np.asarray(T, np.int64)[ok] - ts[idx[ok]]) / H1
    fresh = ok & (age <= FUNDING_MAX_AGE_H)
    for key, arr in per.items():
        v = np.full(len(T), np.nan)
        v[fresh] = arr[idx[fresh]]
        out[key] = v
    out["f_age_h"] = np.where(np.isfinite(out["f_last"]), age, np.nan)
    rs, ms = np.sign(np.asarray(ret_24h, float)), np.sign(out["f_mean9"])
    div = ((rs != 0) & (rs == -ms)).astype(float)
    out["f_div"] = np.where(np.isfinite(out["f_last"]) & np.isfinite(np.asarray(ret_24h, float)), div, np.nan)
    return out


# ── basis: runtime ─────────────────────────────────────────────────────────
def basis_at(perp_bars: dict, index_bars: dict, T: int, ret_24h: float) -> dict:
    """perp_bars / index_bars: {bar_ts: close}. Uses bars with ts + 1h <= T only."""
    out = {k: float("nan") for k in B_KEYS}
    vals = []
    for j in range(168):
        t = int(T) - H1 * (168 - j)                           # bar ts; closes at t + 1h <= T
        p, i = perp_bars.get(t), index_bars.get(t)
        if p is None or i is None or not i:
            return out
        vals.append(float(p) / float(i) - 1.0)
    b = vals[-1]
    m168 = sum(vals) / 168
    sd = math.sqrt(sum((x - m168) ** 2 for x in vals) / 168)
    last24 = vals[-24:]
    m24 = sum(last24) / 24
    xs = list(range(24))
    xm = sum(xs) / 24
    slope = sum((x - xm) * (y - m24) for x, y in zip(xs, last24)) / sum((x - xm) ** 2 for x in xs)
    z = (b - m168) / sd if sd > 0 else 0.0
    out.update({"b_now": b, "b_chg1": b - vals[-2], "b_chg24": b - vals[-25], "b_mean24": m24, "b_z168": z,
                "b_pct168": sum(1 for x in vals if x <= b) / 168 - 0.5, "b_slope24": slope, "b_sign": _sgn(b),
                "b_extreme": 1.0 if abs(z) > 2 else 0.0,
                "b_div": (1.0 if (_sgn(ret_24h) != 0 and _sgn(ret_24h) == -_sgn(m24)) else 0.0)
                if math.isfinite(ret_24h) else float("nan")})
    return out


# ── basis: research (vectorized on the hourly grid) ────────────────────────
def basis_hourly(grid_ts: np.ndarray, perp: dict, index: dict) -> np.ndarray:
    """basis per bar ts in grid (NaN where either completed bar is missing)."""
    p = np.array([perp.get(int(t), np.nan) for t in grid_ts], float)
    i = np.array([index.get(int(t), np.nan) for t in grid_ts], float)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(i > 0, p / i - 1.0, np.nan)


def basis_series(grid_ts: np.ndarray, b: np.ndarray, T: np.ndarray, ret_24h: np.ndarray) -> dict:
    n = len(b)
    per = {k: np.full(n, np.nan) for k in B_KEYS if k != "b_div"}
    if n >= 168:
        w = _swv(b, 168)                                         # row t -> bar j = t + 167
        j = np.arange(167, n)
        last = w[:, -1]
        m168, sd = w.mean(axis=1), w.std(axis=1)
        w24 = w[:, -24:]
        m24 = w24.mean(axis=1)
        x = np.arange(24) - 11.5
        slope = ((w24 - m24[:, None]) * x).sum(axis=1) / (x ** 2).sum()
        z = np.where(sd > 0, (last - m168) / np.where(sd > 0, sd, 1.0), 0.0)
        per["b_now"][j] = last
        per["b_chg1"][j] = last - w[:, -2]
        per["b_chg24"][j] = last - w[:, -25]
        per["b_mean24"][j] = m24
        per["b_z168"][j] = z
        per["b_pct168"][j] = (w <= last[:, None]).sum(axis=1) / 168 - 0.5
        per["b_slope24"][j] = slope
        per["b_sign"][j] = np.sign(last)
        per["b_extreme"][j] = (np.abs(z) > 2).astype(float)
        bad = ~np.isfinite(w).all(axis=1)
        for key in per:
            per[key][j[bad]] = np.nan
    pos = {int(t): k for k, t in enumerate(grid_ts)}
    out = {key: np.full(len(T), np.nan) for key in B_KEYS}
    for q, t in enumerate(np.asarray(T, np.int64)):
        k = pos.get(int(t) - H1)                                # the bar closing exactly at T
        if k is None:
            continue
        for key, arr in per.items():
            out[key][q] = arr[k]
    rs, ms = np.sign(np.asarray(ret_24h, float)), np.sign(out["b_mean24"])
    div = ((rs != 0) & (rs == -ms)).astype(float)
    out["b_div"] = np.where(np.isfinite(out["b_now"]) & np.isfinite(np.asarray(ret_24h, float)), div, np.nan)
    return out


def parity(research: dict, runtime_rows: list, keys) -> dict:
    checked = mism = 0
    worst = 0.0
    ex = []
    for q, rt in runtime_rows:
        for k in keys:
            a, b = float(research[k][q]), float(rt[k])
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
                if len(ex) < 10:
                    ex.append({"q": q, "key": k, "research": a, "runtime": b})
    return {"values_checked": checked, "mismatches": mism, "max_rel_diff": worst, "examples": ex,
            "tolerance": "relative 1e-9 (max(1,|a|,|b|) denominator); NaN must match NaN",
            "parity": checked > 0 and mism == 0}
