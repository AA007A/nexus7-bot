"""Phase 8F: LONG_HORIZON_ARCHITECTURE_SPEC_V1 (research only, strict fail-closed).

Hypothesis under test (NOT assumed): execution cost is large relative to
15m-8h displacement but may be small relative to multi-hour / multi-day
displacement. Direction comes from 1h / 4h triggers with 4h / 1D context
(LONG_HORIZON_FEATURES_V1, research + runtime paths parity-checked); stops
are structurally wide; holding horizons are 12-48h; capital at risk is fixed
at 1R (notional = risk / stop_frac, so a wider stop is always a smaller
position). Funding is charged per actual 8h settlement held.

The architecture population is generated and evaluated in a streaming,
columnar way (per signal), pre-gated on TRAINING folds only, then filtered
by a small model family inside a chronological nested walk-forward. Three
outcomes: CANDIDATE_SUPPORTED / NO_VALID_CHALLENGER / INSUFFICIENT_EVIDENCE.
Evidence label: PREVIOUSLY_INSPECTED_RESEARCH_SUPPORT (never UNTOUCHED_OOS).
"""
from __future__ import annotations

import asyncio
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
from bot.ai import lh_features as lf
from bot.ai import models as mdl

EVIDENCE_LABEL = "PREVIOUSLY_INSPECTED_RESEARCH_SUPPORT"
VERSION = "LONG_HORIZON_ARCHITECTURE_SPEC_V1"
H1 = lf.H1
DAY = 24 * H1
FORWARD_EVIDENCE_CUTOFF_MS = 1_790_000_000_000          # Phase-8 forward window never opened
DECISION_END_MS = 1_787_756_400_000                     # Phase 8D/8E decision end floored to the UTC hour
WINDOW_CANDIDATES_DAYS = (730, 548, 365)                # longest window with exact coverage wins
WARMUP_DAYS = 75                                        # >= 70 complete daily bars before the first decision
MIN_SYMBOLS = 10
MIN_COVERAGE, MAX_GAP_BARS = 0.99, 6
FETCH_PAGE_BARS, EXCHANGE_MAX_KLINES = 150, 200
SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT", "DOGEUSDT", "LINKUSDT", "AVAXUSDT", "DOTUSDT",
           "LTCUSDT", "NEARUSDT", "ATOMUSDT")
MAJORS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")

FAMILIES = ("HTF_TREND_CONTINUATION", "HTF_PULLBACK", "HTF_BREAKOUT", "HTF_MEAN_REVERSION",
            "VOL_COMPRESSION_EXPANSION", "STRUCTURAL_MOMENTUM")
TRIGGERS = ("h1", "h4")                                  # 15m is not used in Phase 8F
CONTEXT = {"h1": ("h4", "d1"), "h4": ("d1",)}
SIDES = ("LONG", "SHORT")
ENTRIES = ("SIGNAL_CLOSE", "ONE_BAR_CONFIRM", "PULLBACK_RETEST", "BREAKOUT_RETEST")
RETEST_BARS = 6
STOPS = ("H1_ATR_2_0", "H1_ATR_3_0", "H4_ATR_1_0", "H4_ATR_1_5", "STRUCT_SWING")
MIN_STOP_FRAC, MAX_STOP_FRAC = 0.005, 0.10
EXITS = ("TGT2R_48H", "TGT3R_48H", "TRAIL_48H", "INVALID_48H", "TIME_12H", "TIME_24H", "TIME_48H")
MAX_HOLD = 48
TRAIL_ATR_MULT = 2.5
HOLDING_HORIZONS_H = (12, 24, 48)

TAKER_FEE, SLIPPAGE_MAJOR, SLIPPAGE_ALT = 0.0006, 0.0005, 0.0010
FUNDING_PER_SETTLEMENT = 0.0001                         # adverse 0.01% at every 00/08/16 UTC settlement held
SETTLEMENT_MS = 8 * H1
UNCERTAINTY_BUFFER_R = 0.05
STRESS = {"fees_x1_5": (1.5, 1.0, 1.0), "slippage_x2": (1.0, 2.0, 1.0), "funding_x2": (1.0, 1.0, 2.0),
          "combined_adverse": (1.5, 2.0, 2.0)}

REQUIRED_MS = 3 * DAY                                   # >= 48h hold + 6h retest + 1h entry delay
PRE = {"min_trades": 60, "min_gross_mean_r": 0.10, "min_months": 3, "min_month_trades": 5, "min_symbols": 3,
       "max_top_symbol_share": 0.5, "max_top_regime_share": 0.6}
MODELS = (("RIDGE_NET", {"l2": 10.0}, (0.0, 0.05, 0.10)), ("LOGISTIC_PROB", {"l2": 1.0}, (0.50, 0.55, 0.60)))
MIN_VALIDATION_TRADES = 30
GATE = {"min_pooled_trades": 60, "max_top_symbol_share": 0.5, "max_top_month_share": 0.5,
        "max_top_quarter_share": 0.6, "max_top_regime_share": 0.6, "max_top_week_share": 0.35, "max_ece": 0.10}
PORTFOLIO = {"start_equity": 10_000.0, "risk_per_trade": 0.01, "max_concurrent": 4, "max_per_symbol": 1,
             "max_open_risk": 0.04, "max_gross_notional_x_equity": 5.0}

SPEC = {
    "version": VERSION, "evidence_label": EVIDENCE_LABEL, "forward_evidence_cutoff_ms": FORWARD_EVIDENCE_CUTOFF_MS,
    "data": {"source": "KuCoin Futures public 1h klines; 4h/1D = complete UTC-aligned 1h groups",
             "symbols": list(SYMBOLS), "decision_end_ms": DECISION_END_MS,
             "window_candidates_days": list(WINDOW_CANDIDATES_DAYS), "warmup_days": WARMUP_DAYS,
             "window_rule": f"longest candidate window in which >= {MIN_SYMBOLS} symbols have >= {MIN_COVERAGE} 1h "
                            f"coverage and no gap > {MAX_GAP_BARS} bars; only passing symbols are used",
             "pagination": f"<= {FETCH_PAGE_BARS} bars per request (exchange max {EXCHANGE_MAX_KLINES}); every "
                           "returned timestamp verified"},
    "features": {"schema": lf.VERSION, "schema_sha256": lf.SCHEMA_SHA256},
    "decision_time": "open of 1h bar i; 1h trigger decides every hour, 4h trigger only at 4h boundaries",
    "families": {
        "HTF_TREND_CONTINUATION": "every context trend == s & trig.trend == s & s*trig.roc10 > 0",
        "HTF_PULLBACK": "ctx0.trend == s & trig touched sma20 within 10 bars & s*(trig.c-trig.sma20) > 0 & trig.body == s",
        "HTF_BREAKOUT": "trig.c beyond trig donchian20 in s & trig.range_ratio >= 1.5 & ctx0.trend != -s",
        "HTF_MEAN_REVERSION": "ctx0.range == 1 & s*(trig.c-trig.sma20)/trig.std20 < -2",
        "VOL_COMPRESSION_EXPANSION": "trig.atr_rank50 <= 0.2 & trig.range_ratio >= 2 & trig.body == s",
        "STRUCTURAL_MOMENTUM": "ctx0.structure == s & trig.structure == s & s*trig.roc10 > 0",
        "context": {"h1": ["h4", "d1"], "h4": ["d1"]}, "event": "condition true now, false at previous decision"},
    "entries": {"SIGNAL_CLOSE": "market at open of the decision bar",
                "ONE_BAR_CONFIRM": "next 1h bar closes in s -> market at following open, else cancel",
                "PULLBACK_RETEST": f"level = trig.c - s*0.5*trig.atr14; first 1h bar in {RETEST_BARS} touching it and "
                                   "closing beyond it in s -> market at next open, else cancel",
                "BREAKOUT_RETEST": "trig.c must already be beyond its prior donchian20 level; first 1h bar in "
                                   f"{RETEST_BARS} touching that level and closing beyond it -> market at next open"},
    "stops": {"H1_ATR_2_0": "2.0*h1.atr14", "H1_ATR_3_0": "3.0*h1.atr14", "H4_ATR_1_0": "1.0*h4.atr14",
              "H4_ATR_1_5": "1.5*h4.atr14",
              "STRUCT_SWING": "max(beyond h4 10-bar swing + 0.25*h4.atr14, 1.0*h4.atr14)",
              "min_stop_frac": MIN_STOP_FRAC, "max_stop_frac": MAX_STOP_FRAC,
              "sizing": "fixed 1R capital at risk; notional = risk/stop_frac"},
    "exits": {"TGT2R_48H": "stop / +2R / close of hour 48", "TGT3R_48H": "stop / +3R / close of hour 48",
              "TRAIL_48H": f"stop trails max(close since entry) - {TRAIL_ATR_MULT}*h1.atr14(entry); hour 48",
              "INVALID_48H": "exit at next 1h open once the last closed 4h close is beyond 4h sma20 against s; hour 48",
              "TIME_12H": "stop / close of hour 12", "TIME_24H": "stop / close of hour 24",
              "TIME_48H": "stop / close of hour 48", "holding_horizons_h": list(HOLDING_HORIZONS_H),
              "intrabar": "stop before target/trail in the same bar; gap through stop fills at the open",
              "overlap": "one open trade per (symbol, architecture tuple)"},
    "costs": {"taker_fee_per_side": TAKER_FEE, "slippage_per_side": {"majors": SLIPPAGE_MAJOR, "alts": SLIPPAGE_ALT},
              "funding": f"{FUNDING_PER_SETTLEMENT} adverse per 00/08/16 UTC settlement in (entry, exit]",
              "uncertainty_buffer_r": UNCERTAINTY_BUFFER_R, "stress": {k: list(v) for k, v in STRESS.items()}},
    "pre_gate_training_only": PRE,
    "models": [[m, hp, list(th)] for m, hp, th in MODELS],
    "model_rules": {"RIDGE_NET": "ridge on realized NET R; approve predicted > threshold",
                    "LOGISTIC_PROB": "logistic on NET R > 0 + Platt on validation; authorizes only if validation "
                                     "calibration beats base-rate Brier"},
    "walk_forward": {"folds": 4, "purge_horizon_ms": REQUIRED_MS,
                     "outer_steps": [{"train": [1], "validate": 2, "evaluate": 3},
                                     {"train": [1, 2], "validate": 3, "evaluate": 4}],
                     "selection": f"max validation mean NET R, >= {MIN_VALIDATION_TRADES} trades and mean > 0"},
    "supporting_gate": {**GATE, "stable": "same trigger timeframe, family and side in both steps",
                        "cost_stress_positive": list(STRESS), "expectancy_and_uplift_block_ci_low_gt_0": True,
                        "runtime_feature_parity": True, "runtime_architecture_parity": True,
                        "deterministic_export_load_inference": True},
    "classification": {
        "CANDIDATE_SUPPORTED": "every supporting gate passes",
        "INSUFFICIENT_EVIDENCE": "gate failures are only sample-size failures (too few pooled trades, block CI not "
                                 "estimable) with positive point expectancy; or no selection while fewer than 50% of "
                                 "tuples with trades reach the pre-gate minimum training sample in some step "
                                 "(population too small to evaluate)",
        "NO_VALID_CHALLENGER": "otherwise"},
    "portfolio_policy": PORTFOLIO,
}
SPEC_SHA256 = hashlib.sha256(json.dumps(SPEC, sort_keys=True).encode()).hexdigest()
COST_POLICY_SHA256 = hashlib.sha256(json.dumps(SPEC["costs"], sort_keys=True).encode()).hexdigest()
EXIT_POLICY_SHA256 = hashlib.sha256(json.dumps(SPEC["exits"], sort_keys=True).encode()).hexdigest()
PORTFOLIO_POLICY_SHA256 = hashlib.sha256(json.dumps(PORTFOLIO, sort_keys=True).encode()).hexdigest()


class ForwardEvidenceRefused(ValueError):
    pass


# ── data ───────────────────────────────────────────────────────────────────
assert DECISION_END_MS % (24 * 3_600_000) % 3_600_000 == 0


def coverage(bars, start: int, end: int, step: int = H1) -> dict:
    ts = [int(b["ts"]) for b in bars if start <= int(b["ts"]) < end]
    expected = max(1, (end - start) // step)
    gaps = [(b - a) // step - 1 for a, b in zip(ts, ts[1:])]
    lead = (ts[0] - start) // step if ts else expected
    tail = (end - step - ts[-1]) // step if ts else expected
    max_gap = max(gaps + [lead, tail]) if ts else expected
    frac = len(ts) / expected
    return {"bars": len(ts), "expected": expected, "fraction": frac, "max_gap_bars": int(max_gap),
            "missing_bars": int(expected - len(ts)), "first_ts": ts[0] if ts else None, "last_ts": ts[-1] if ts else None,
            "ok": bool(ts) and frac >= MIN_COVERAGE and max_gap <= MAX_GAP_BARS
            and len(set(ts)) == len(ts) and ts == sorted(ts)}


def select_window(raw: dict) -> dict:
    """Longest predeclared window with exact coverage (coverage only, never outcomes)."""
    report = []
    for days in WINDOW_CANDIDATES_DAYS:
        start = DECISION_END_MS - days * DAY
        cov = {s: coverage(raw[s], start - WARMUP_DAYS * DAY, DECISION_END_MS + 3 * DAY) for s in sorted(raw)}
        passing = [s for s, c in cov.items() if c["ok"]]
        report.append({"days": days, "passing_symbols": passing, "coverage": cov})
        if len(passing) >= MIN_SYMBOLS:
            return {"status": "OK", "days": days, "decision_start_ms": start, "decision_end_ms": DECISION_END_MS,
                    "symbols": passing, "candidates": report}
    return {"status": "INSUFFICIENT_COVERAGE", "candidates": report}


async def fetch_series(page_fn, symbol: str, start: int, end: int, *, step: int = H1) -> tuple[list, dict]:
    """Forward pagination; never trusts the requested page size."""
    cur, by, pages, max_ret = start, {}, 0, 0
    while cur < end:
        to = min(end, cur + FETCH_PAGE_BARS * step)
        page = await page_fn(symbol, cur, to)
        pages += 1
        max_ret = max(max_ret, len(page))
        if len(page) > EXCHANGE_MAX_KLINES:
            raise RuntimeError("page larger than the exchange maximum")
        for b in page:
            t = int(b["ts"])
            if cur <= t <= to:
                by[t] = b
        cur = to
    bars = [by[t] for t in sorted(by) if start <= t and t + step <= end]
    return ([{k: (int(b[k]) if k == "ts" else float(b[k])) for k in ("ts", "o", "h", "l", "c", "v")} for b in bars],
            {"pages": pages, "max_returned_per_page": max_ret, "requested_bars_per_page": FETCH_PAGE_BARS})


async def fetch_dataset(symbols=SYMBOLS) -> tuple[dict, dict]:
    from bot.backtest import _kucoin_page
    from bot.nexus_oos_real_replay import PublicKuCoinFuturesClient
    start = DECISION_END_MS - (max(WINDOW_CANDIDATES_DAYS) + WARMUP_DAYS) * DAY
    end = min(DECISION_END_MS + 3 * DAY, FORWARD_EVIDENCE_CUTOFF_MS)
    out, meta = {}, {}
    async with PublicKuCoinFuturesClient() as client:
        async def page(sym, a, b):
            await asyncio.sleep(0.05)
            return await _kucoin_page(client, sym, "60", a, b)
        for s in symbols:
            out[s], meta[s] = await fetch_series(page, s, start, end)
    return out, meta


def dataset_sha256(data: dict) -> str:
    canon = {s: [[int(b["ts"]), float(b["o"]), float(b["h"]), float(b["l"]), float(b["c"]), float(b["v"])]
                 for b in data[s]] for s in sorted(data)}
    return hashlib.sha256(json.dumps(canon, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def save_dataset(data: dict, path, meta=None) -> str:
    sha = dataset_sha256(data)
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        json.dump({"header": {"version": VERSION, "sha256": sha, "symbols": sorted(data), "meta": meta or {}},
                   "data": {s: [[int(b["ts"]), b["o"], b["h"], b["l"], b["c"], b["v"]] for b in data[s]]
                            for s in sorted(data)}}, fh, sort_keys=True)
    return sha


def load_dataset(path) -> tuple[dict, dict]:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        raw = json.load(fh)
    data = {s: [{"ts": int(b[0]), "o": float(b[1]), "h": float(b[2]), "l": float(b[3]), "c": float(b[4]),
                 "v": float(b[5])} for b in bars] for s, bars in raw["data"].items()}
    if dataset_sha256(data) != raw["header"]["sha256"]:
        raise ValueError("dataset hash mismatch")
    return data, raw["header"]


def check_no_forward_evidence(data: dict):
    for s, bars in data.items():
        if bars and int(bars[-1]["ts"]) >= FORWARD_EVIDENCE_CUTOFF_MS:
            raise ForwardEvidenceRefused(f"{s}: candles at/after the forward-evidence cutoff")


# ── signals / entries / stops ──────────────────────────────────────────────
def condition(family: str, F: dict, s: float, trig: str):
    """Elementwise on arrays (research) or scalars (runtime dict). NaN -> False."""
    g = lambda k: np.asarray(F[k], float)                   # noqa: E731
    p = f"{trig}_"
    ctx = CONTEXT[trig]
    c0 = f"{ctx[0]}_"
    with np.errstate(invalid="ignore", divide="ignore"):
        if family == "HTF_TREND_CONTINUATION":
            out = (g(p + "trend") == s) & (s * g(p + "roc10") > 0)
            for c in ctx:
                out = out & (g(f"{c}_trend") == s)
        elif family == "HTF_PULLBACK":
            touch = (g(p + "lo10") <= g(p + "sma20")) if s > 0 else (g(p + "hi10") >= g(p + "sma20"))
            out = (g(c0 + "trend") == s) & touch & (s * (g(p + "c") - g(p + "sma20")) > 0) & (g(p + "body") == s)
        elif family == "HTF_BREAKOUT":
            brk = (g(p + "c") > g(p + "donhi20")) if s > 0 else (g(p + "c") < g(p + "donlo20"))
            out = brk & (g(p + "range_ratio") >= 1.5) & (g(c0 + "trend") != -s) & ~np.isnan(g(c0 + "trend"))
        elif family == "HTF_MEAN_REVERSION":
            std = g(p + "std20")
            z = np.where(std > 0, (g(p + "c") - g(p + "sma20")) / np.where(std > 0, std, 1.0), np.nan)
            out = (g(c0 + "range") == 1.0) & (s * z < -2.0)
        elif family == "VOL_COMPRESSION_EXPANSION":
            out = (g(p + "atr_rank50") <= 0.2) & (g(p + "range_ratio") >= 2.0) & (g(p + "body") == s)
        elif family == "STRUCTURAL_MOMENTUM":
            out = (g(c0 + "structure") == s) & (g(p + "structure") == s) & (s * g(p + "roc10") > 0)
        else:
            raise ValueError(family)
    return out


def decision_indices(ts: np.ndarray, trig: str) -> np.ndarray:
    return np.arange(len(ts)) if trig == "h1" else np.flatnonzero(ts % lf.TF_MS["h4"] == 0)


def events(F: dict, family: str, s: float, trig: str) -> np.ndarray:
    c = np.asarray(condition(family, F, s, trig), bool)
    d = decision_indices(F["ts"], trig)
    ev = np.zeros(len(c), bool)
    if len(d) > 1:
        ev[d[1:]] = c[d[1:]] & ~c[d[:-1]]
    return ev


def entry_index(arr, F, i: int, entry: str, s: float, trig: str):
    o, h, lo, c = arr
    n = len(o)
    p = f"{trig}_"
    if entry == "SIGNAL_CLOSE":
        return i
    if entry == "ONE_BAR_CONFIRM":
        return i + 1 if i < n and s * (c[i] - o[i]) > 0 else None
    if entry == "PULLBACK_RETEST":
        atr = float(F[p + "atr14"][i])
        if not atr > 0:
            return None
        lvl = float(F[p + "c"][i]) - s * 0.5 * atr
    elif entry == "BREAKOUT_RETEST":
        lvl = float(F[p + ("donhi20" if s > 0 else "donlo20")][i])
        if not (math.isfinite(lvl) and s * (float(F[p + "c"][i]) - lvl) > 0):
            return None
    else:
        raise ValueError(entry)
    for k in range(i, min(n, i + RETEST_BARS)):
        if (lo[k] <= lvl < c[k]) if s > 0 else (h[k] >= lvl > c[k]):
            return k + 1
    return None


def stop_distance(stop: str, F, j: int, s: float, e: float):
    a1, a4 = float(F["h1_atr14"][j]), float(F["h4_atr14"][j])
    if stop == "H1_ATR_2_0":
        d = 2.0 * a1
    elif stop == "H1_ATR_3_0":
        d = 3.0 * a1
    elif stop == "H4_ATR_1_0":
        d = a4
    elif stop == "H4_ATR_1_5":
        d = 1.5 * a4
    elif stop == "STRUCT_SWING":
        ext = float(F["h4_lo10"][j] if s > 0 else F["h4_hi10"][j])
        d = max(s * (e - ext) + 0.25 * a4, a4) if math.isfinite(ext) else float("nan")
    else:
        raise ValueError(stop)
    return d if (math.isfinite(d) and d > 0) else None


def cost_fracs(symbol: str) -> dict:
    slip = SLIPPAGE_MAJOR if symbol in MAJORS else SLIPPAGE_ALT
    return {"fee": 2 * TAKER_FEE, "slip": 2 * slip}


def settlements(entry_ts, exit_ts):
    return np.floor_divide(np.asarray(exit_ts, np.int64), SETTLEMENT_MS) - np.floor_divide(np.asarray(entry_ts, np.int64),
                                                                                             SETTLEMENT_MS)


# ── vectorized simulation of every exit ────────────────────────────────────
def simulate(arr, ts, F, J, S, E, D):
    """All EXITS for N entries at 1h bar J (open), direction S, entry E, distance D."""
    o, h, lo, c = arr
    N = len(J)
    Ow, Hw, Lw, Cw = (_swv(x, MAX_HOLD)[J] for x in (o, h, lo, c))
    Sx, Ex, Dx = S[:, None], E[:, None], D[:, None]
    adv = Sx * (np.where(Sx > 0, Lw, Hw) - Ex) / Dx
    fav = Sx * (np.where(Sx > 0, Hw, Lw) - Ex) / Dx
    cR = Sx * (Cw - Ex) / Dx
    oR = Sx * (Ow - Ex) / Dx
    BIG = MAX_HOLD + 10
    rows = np.arange(N)

    def first(mask):
        any_ = mask.any(axis=1)
        return np.where(any_, mask.argmax(axis=1), BIG)
    k_stop = first(adv <= -1.0)
    stop_g = np.minimum(-1.0, oR[rows, np.minimum(k_stop, MAX_HOLD - 1)])
    out = {}

    def fixed(target, horizon):
        k_t = first(fav >= target) if target else np.full(N, BIG)
        k_h = horizon - 1
        stop_first = k_stop <= np.minimum(k_t, k_h)
        tgt_first = ~stop_first & (k_t <= k_h)
        k = np.where(stop_first, k_stop, np.where(tgt_first, k_t, k_h))
        g = np.where(stop_first, stop_g, np.where(tgt_first, float(target or 0), cR[:, k_h]))
        return g, k, np.zeros(N, bool)
    out["TGT2R_48H"] = fixed(2.0, 48)
    out["TGT3R_48H"] = fixed(3.0, 48)
    for H in HOLDING_HORIZONS_H:
        out[f"TIME_{H}H"] = fixed(None, H)
    # trailing stop: level for bar k from closes of bars < k (in R, direction-signed)
    m = TRAIL_ATR_MULT * np.asarray(F["h1_atr14"], float)[J] / D
    prev_max = np.maximum.accumulate(np.concatenate([np.full((N, 1), -np.inf), cR[:, :-1]], axis=1), axis=1)
    eff = np.maximum(-1.0, prev_max - m[:, None])
    k_tr = first(adv <= eff)
    g_tr = np.minimum(eff[rows, np.minimum(k_tr, MAX_HOLD - 1)], oR[rows, np.minimum(k_tr, MAX_HOLD - 1)])
    hit = k_tr < MAX_HOLD
    out["TRAIL_48H"] = (np.where(hit, g_tr, cR[:, -1]), np.where(hit, k_tr, MAX_HOLD - 1), np.zeros(N, bool))
    # trend invalidation: exit at the open of bar k (k >= 1) when the last closed 4h bar is against s
    h4c, h4s = (np.asarray(F[k], float) for k in ("h4_c", "h4_sma20"))
    Kidx = J[:, None] + np.arange(MAX_HOLD)[None, :]
    with np.errstate(invalid="ignore"):
        inv = Sx * (h4c[Kidx] - h4s[Kidx]) < 0
    inv[:, 0] = False
    k_inv = first(inv)
    inv_first = k_inv <= k_stop
    k_iv = np.where(inv_first, k_inv, np.where(k_stop < BIG, k_stop, MAX_HOLD - 1))
    g_iv = np.where(inv_first, oR[rows, np.minimum(k_inv, MAX_HOLD - 1)],
                    np.where(k_stop < BIG, stop_g, cR[:, -1]))
    out["INVALID_48H"] = (g_iv, k_iv, inv_first & (k_inv < BIG))
    # path metrics over the full 48h (independent of exit)
    before = {}
    for lvl in (0.5, 1.0, 1.5, 2.0):
        k_l = first(fav >= lvl)
        before[lvl] = k_l < k_stop
    path = {"mfe_r": np.maximum(fav.max(axis=1), 0.0), "mae_r": np.minimum(adv.min(axis=1), 0.0),
            "t_mfe_h": fav.argmax(axis=1) + 1, "t_mae_h": adv.argmin(axis=1) + 1, "iae_r": np.minimum(adv[:, 0], 0.0),
            "stop_touched": k_stop < BIG, "stop_first": (k_stop < BIG) & ~before[1.0],
            "plus05_before_stop": before[0.5], "plus1_before_stop": before[1.0],
            "plus15_before_stop": before[1.5], "plus2_before_stop": before[2.0],
            **{f"abs_move_r_{H}h": np.abs(cR[:, H - 1]) for H in HOLDING_HORIZONS_H}}
    res = {}
    for ex, (g, k, at_open) in out.items():
        k = k.astype(np.int64)
        exit_ts = ts[J + k] + np.where(at_open, 0, H1)
        res[ex] = {"gross_r": g.astype(float), "k_exit": J + k, "exit_ts": exit_ts}
    return res, path


# ── columnar generation (streaming per signal) ─────────────────────────────
def tuple_id(trig, fam, side, entry, stop, exit_):
    return f"{trig}|{fam}|{side}|{entry}|{stop}|{exit_}"


def signals():
    for trig in TRIGGERS:
        for fam in FAMILIES:
            for side in SIDES:
                yield trig, fam, side


def architecture_count():
    return len(TRIGGERS) * len(FAMILIES) * len(SIDES) * len(ENTRIES) * len(STOPS) * len(EXITS)


def _regime(F, j):
    t, r = F["d1_trend"][j], F["d1_range"][j]
    return np.where(t == 1, "TREND_UP", np.where(t == -1, "TREND_DOWN", np.where(r == 1, "RANGE", "MIXED")))


def _month(ts):
    return np.array([dt.datetime.fromtimestamp(int(t) / 1000, dt.timezone.utc).strftime("%Y-%m") for t in ts])


def generate_signal(sym_ctx: dict, trig, fam, side, *, span, entries=ENTRIES, stops=STOPS, exits=EXITS) -> dict:
    """{tuple_id: columnar block} for one signal across all symbols."""
    s = 1.0 if side == "LONG" else -1.0
    blocks = defaultdict(list)
    for sym in sorted(sym_ctx):
        ctx = sym_ctx[sym]
        F, arr, ts = ctx["F"], ctx["arr"], ctx["F"]["ts"]
        n = len(ts)
        ev = ctx["events"].get((trig, fam, side))
        if ev is None:
            ev = events(F, fam, s, trig)
        idx = [int(i) for i in np.flatnonzero(ev) if span[0] <= ts[i] < span[1] and i > lf.RUNTIME_1H_BARS]
        cf = cost_fracs(sym)
        for en in entries:
            pairs = []
            for i in idx:
                j = entry_index(arr, F, i, en, s, trig)
                if j is not None and j + MAX_HOLD <= n:
                    pairs.append((i, j))
            if not pairs:
                continue
            Iv = np.array([p[0] for p in pairs])
            J = np.array([p[1] for p in pairs])
            E = arr[0][J]
            for st in stops:
                D = np.array([stop_distance(st, F, int(j), s, float(e)) or np.nan for j, e in zip(J, E)])
                sf = D / E
                ok = np.isfinite(D) & (sf >= MIN_STOP_FRAC) & (sf <= MAX_STOP_FRAC)
                if not ok.any():
                    continue
                Ii, Jj, Ee, Dd = Iv[ok], J[ok], E[ok], D[ok]
                sim, path = simulate(arr, ts, F, Jj, np.full(len(Jj), s), Ee, Dd)
                sfo = Dd / Ee
                X_all = np.array([lf.model_vector(F, int(j), s, trig, float(q), cf["fee"] + cf["slip"])
                                  for j, q in zip(Jj, sfo)])
                for ex in exits:
                    r = sim[ex]
                    keep, free = [], -1
                    for q in range(len(Jj)):
                        if Ii[q] <= free or Jj[q] <= free:
                            continue
                        keep.append(q)
                        free = int(r["k_exit"][q])
                    if not keep:
                        continue
                    kq = np.array(keep)
                    jj = Jj[kq]
                    ent_ts, ex_ts = ts[jj], r["exit_ts"][kq]
                    nset = settlements(ent_ts, ex_ts)
                    sfk = sfo[kq]
                    fee, slip = cf["fee"] / sfk, cf["slip"] / sfk
                    fund = nset * FUNDING_PER_SETTLEMENT / sfk
                    gross = r["gross_r"][kq]
                    blk = {"ts": ent_ts, "event_ts": ts[Ii[kq]], "end_ts": ex_ts, "symbol": np.full(len(kq), sym),
                           "gross_r": gross, "fee_r": fee, "slip_r": slip, "fund_r": fund, "n_settle": nset,
                           "stop_frac": sfk, "hold_h": (ex_ts - ent_ts) / H1, "j": jj,
                           "regime": _regime(F, jj), **{k: v[kq] for k, v in path.items()}}
                    for name, (a, b, cc) in STRESS.items():
                        blk[name] = gross - a * fee - b * slip - cc * fund - UNCERTAINTY_BUFFER_R
                    blk["r"] = gross - fee - slip - fund - UNCERTAINTY_BUFFER_R
                    blk["x"] = X_all[kq]
                    blocks[tuple_id(trig, fam, side, en, st, ex)].append(blk)
    out = {}
    for tid, bl in blocks.items():
        cat = {k: np.concatenate([b[k] for b in bl]) for k in bl[0]}
        order = np.lexsort((cat["symbol"], cat["ts"]))
        cat = {k: v[order] for k, v in cat.items()}
        cat["month"] = _month(cat["ts"])
        out[tid] = cat
    return out


def symbol_context(data: dict) -> dict:
    ctx = {}
    for s in sorted(data):
        c1h = data[s]
        F = lf.series(c1h)
        arr = tuple(np.array([float(b[k]) for b in c1h]) for k in ("o", "h", "l", "c"))
        ctx[s] = {"F": F, "arr": arr, "events": {}}
    return ctx


# ── statistics ─────────────────────────────────────────────────────────────
def stats(x) -> dict:
    x = np.asarray(x, float)
    n = len(x)
    if not n:
        return {"n": 0}
    w, lo = x[x > 0], -x[x < 0]
    return {"n": int(n), "mean_r": float(x.mean()), "median_r": float(np.median(x)), "win_rate": float((x > 0).mean()),
            "profit_factor": float(w.sum() / lo.sum()) if lo.sum() > 0 else None,
            "avg_win_r": float(w.mean()) if len(w) else None, "avg_loss_r": float(-lo.mean()) if len(lo) else None,
            "total_r": float(x.sum())}


def share(values, r) -> dict:
    tot = defaultdict(float)
    for k, v in zip(values, r):
        tot[str(k)] += float(v)
    pos = {k: v for k, v in tot.items() if v > 0}
    ps = sum(pos.values())
    top = max(pos.items(), key=lambda kv: kv[1]) if pos else (None, 0.0)
    return {"groups": len(tot), "groups_positive": len(pos), "top_group": top[0],
            "top_share_of_positive_r": (top[1] / ps) if ps > 0 else None}


def _quarter(month):
    y, m = month.split("-")
    return f"{y}-Q{(int(m) - 1) // 3 + 1}"


def _week(ts):
    d = dt.datetime.fromtimestamp(int(ts) / 1000, dt.timezone.utc).isocalendar()
    return f"{d[0]}-W{d[1]:02d}"


def cost_diag(b, mask=None) -> dict:
    m = slice(None) if mask is None else mask
    tot = b["fee_r"][m] + b["slip_r"][m] + b["fund_r"][m] + UNCERTAINTY_BUFFER_R
    tc = float(tot.mean()) if len(tot) else None
    g = float(b["gross_r"][m].mean()) if len(tot) else None
    mv = float(np.abs(b["gross_r"][m]).mean()) if len(tot) else None
    return {"fee_r": float(b["fee_r"][m].mean()) if len(tot) else None,
            "slippage_r": float(b["slip_r"][m].mean()) if len(tot) else None,
            "funding_r": float(b["fund_r"][m].mean()) if len(tot) else None,
            "uncertainty_buffer_r": UNCERTAINTY_BUFFER_R, "total_cost_r": tc,
            "gross_edge_to_cost": (g / tc) if tc else None, "abs_move_to_cost": (mv / tc) if tc else None,
            "mean_stop_frac": float(b["stop_frac"][m].mean()) if len(tot) else None,
            "mean_notional_per_risk": float((1.0 / b["stop_frac"][m]).mean()) if len(tot) else None,
            "mean_settlements": float(b["n_settle"][m].mean()) if len(tot) else None}


# ── pre-gate (training folds only) ─────────────────────────────────────────
SAMPLE_FAILURES = {"TOO_FEW_TRADES", "TOO_FEW_MONTHS", "TOO_FEW_SYMBOLS"}


def pre_gate(b, m) -> dict:
    r = b["r"][m]
    n = len(r)
    fails = []
    if n < PRE["min_trades"]:
        fails.append("TOO_FEW_TRADES")
    mean = float(r.mean()) if n else None
    if not (mean is not None and mean > 0):
        fails.append("NET_MEAN_NOT_POSITIVE")
    ca = float(b["combined_adverse"][m].mean()) if n else None
    if not (ca is not None and ca > 0):
        fails.append("COMBINED_ADVERSE_NOT_POSITIVE")
    g = float(b["gross_r"][m].mean()) if n else None
    if not (g is not None and g >= PRE["min_gross_mean_r"]):
        fails.append("GROSS_EDGE_TOO_SMALL")
    months = defaultdict(list)
    for mo, v in zip(b["month"][m], r):
        months[mo].append(v)
    big = {k: v for k, v in months.items() if len(v) >= PRE["min_month_trades"]}
    if len(big) < PRE["min_months"]:
        fails.append("TOO_FEW_MONTHS")
    elif sum(np.mean(v) > 0 for v in big.values()) * 2 < len(big):
        fails.append("NOT_POSITIVE_ACROSS_MONTHS")
    if len(set(b["symbol"][m])) < PRE["min_symbols"]:
        fails.append("TOO_FEW_SYMBOLS")
    for key, lim, code in (("symbol", PRE["max_top_symbol_share"], "ONE_SYMBOL"),
                           ("regime", PRE["max_top_regime_share"], "ONE_REGIME")):
        sh = share(b[key][m], r)["top_share_of_positive_r"] if n else None
        if n and (sh is None or sh > lim):
            fails.append(code)
    return {"pass": not fails, "failures": fails, "n": n, "net_mean_r": mean, "combined_adverse_mean_r": ca,
            "gross_mean_r": g, "only_sample_failures": bool(fails) and set(fails) <= SAMPLE_FAILURES}


# ── folds / models / search ────────────────────────────────────────────────
STEPS = (([0], 1, 2), ([0, 1], 2, 3))


def fold_layout(start: int, end: int):
    lay = inf.purged_calendar_folds(int(start), int(end), required_horizon_ms=REQUIRED_MS)
    return lay


def fold_of(b, lay) -> np.ndarray:
    f = np.full(len(b["ts"]), -1)
    for k, w in enumerate(lay["windows"]):
        m = (b["ts"] >= w["decision_start_ts"]) & (b["ts"] < w["decision_end_ts"]) & (b["end_ts"] < w["outcome_window_end_ts"])
        f[m] = k
    return f


class FittedModel:
    def __init__(self, kind, hp, threshold, model, calibrator=None):
        self.kind, self.hp, self.threshold, self.model, self.calibrator = kind, dict(hp), float(threshold), model, calibrator

    def score(self, X):
        X = np.asarray(X, float).reshape(-1, len(lf.MODEL_FEATURES))
        if self.kind == "RIDGE_NET":
            return np.asarray(self.model.predict(X), float)
        p = np.asarray(self.model.predict_proba(X), float)
        return np.asarray(self.calibrator.transform(p), float) if self.calibrator is not None else p

    def approve_mask(self, X):
        if len(X) == 0:
            return np.zeros(0, bool)
        sc = self.score(X)
        return sc > self.threshold if self.kind == "RIDGE_NET" else sc >= self.threshold

    def to_json(self):
        return {"kind": self.kind, "hp": self.hp, "threshold": self.threshold, "model": self.model.params(),
                "calibration": self.calibrator.to_json() if self.calibrator is not None else None,
                "feature_schema": lf.VERSION, "feature_schema_sha256": lf.SCHEMA_SHA256,
                "features": list(lf.MODEL_FEATURES)}

    @classmethod
    def from_json(cls, j):
        model = (mdl.Ridge if j["kind"] == "RIDGE_NET" else mdl.LogisticL2).from_params(j["model"])
        return cls(j["kind"], j["hp"], j["threshold"], model, cal.from_json(j["calibration"]) if j.get("calibration") else None)


def _fit_models(Xt, rt, Xv, rv):
    out = []
    for kind, hp, grid in MODELS:
        if kind == "RIDGE_NET":
            m = mdl.Ridge(**hp).fit(Xt, rt)
            out += [(FittedModel(kind, hp, th, m), None) for th in grid]
        else:
            yb = (rt > 0).astype(float)
            if yb.min() == yb.max():
                continue
            m = mdl.LogisticL2(**hp).fit(Xt, yb)
            yv = (rv > 0).astype(float)
            c = cal.Platt().fit(m.predict_proba(Xv), yv) if len(yv) and 0 < yv.mean() < 1 else None
            rep = cal.report(c.transform(m.predict_proba(Xv)) if c else m.predict_proba(Xv), yv) if len(yv) else None
            out += [(FittedModel(kind, hp, th, m, c), rep) for th in grid]
    return out


def _rows(b, mask) -> list[dict]:
    idx = np.flatnonzero(mask)
    keys = ("ts", "end_ts", "symbol", "month", "regime", "r", "gross_r", "hold_h", *STRESS)
    out = []
    for i in idx:
        d = {k: (b[k][i].item() if hasattr(b[k][i], "item") else b[k][i]) for k in keys}
        d.update({"outcome_end_ts": d["end_ts"], "outcome_status": "RESOLVED", "ai_regime": d["regime"],
                  "quarter": _quarter(d["month"]), "week": _week(d["ts"]),
                  "stop_frac": float(b["stop_frac"][i]), "cost_r": {k: float(b[k][i]) for k in STRESS}})
        out.append(d)
    return out


def search(kept: dict, ledger_raw: list) -> dict:
    ledger = list(ledger_raw)
    steps = []
    for si, (tr_idx, va, ev) in enumerate(STEPS):
        best = None
        for tid in sorted(kept):
            b = kept[tid]
            if not b["pre"][si]["pass"]:
                continue
            f = b["fold"]
            mt, mv, me = np.isin(f, tr_idx), f == va, f == ev
            for fm, rep in _fit_models(b["x"][mt], b["r"][mt], b["x"][mv], b["r"][mv]):
                reason = "OK"
                if fm.kind == "LOGISTIC_PROB" and not (rep and rep.get("beats_base_rate")):
                    reason = "CALIBRATION_DOES_NOT_BEAT_BASE_RATE"
                av = fm.approve_mask(b["x"][mv]) if reason == "OK" else np.zeros(int(mv.sum()), bool)
                ao = fm.approve_mask(b["x"][me]) if reason == "OK" else np.zeros(int(me.sum()), bool)
                rv, ro = b["r"][mv][av], b["r"][me][ao]
                vm = float(rv.mean()) if len(rv) else None
                ok = reason == "OK" and len(rv) >= MIN_VALIDATION_TRADES and vm is not None and vm > 0
                if reason == "OK" and not ok:
                    reason = "VALIDATION_TOO_FEW_OR_NOT_POSITIVE"
                ledger.append({"step": si + 1, "tuple": tid, "candidate_id": f"{tid}|{fm.kind}|{fm.threshold}",
                               "model": fm.kind, "threshold": fm.threshold, "promotable": True, "reason": reason,
                               "search_spec_sha256": SPEC_SHA256,
                               "validation": {"n": int(len(rv)), "mean_r": vm,
                                              "calibration": {k: (rep or {}).get(k) for k in
                                                              ("brier", "brier_base_rate", "ece", "beats_base_rate")}},
                               "outer_posthoc_not_used_for_selection": stats(ro), "selected": False})
                if ok and (best is None or vm > best[0]):
                    best = (vm, len(ledger) - 1, fm, tid, int(len(rv)))
        if best is None:
            steps.append({"step": si + 1, "selected": None, "reason": "NO_CANDIDATE_PASSES_PREGATE_AND_VALIDATION",
                          "test": [], "approved": []})
            continue
        vm, li, fm, tid, nval = best
        ledger[li]["selected"] = True
        b = kept[tid]
        me = b["fold"] == ev
        ao = fm.approve_mask(b["x"][me])
        test = _rows(b, me)
        appr = [t for t, a in zip(test, ao) if a]
        trig, fam, side, en, st, ex = tid.split("|")
        cal_rep = None
        if fm.kind == "LOGISTIC_PROB" and me.any():
            cal_rep = cal.report(fm.score(b["x"][me]), (b["r"][me] > 0).astype(float))
        steps.append({"step": si + 1, "selected": {"candidate_id": ledger[li]["candidate_id"], "tuple": tid,
                                                   "trigger": trig, "family": fam, "side": side, "entry": en,
                                                   "stop": st, "exit": ex, "model": fm.to_json(),
                                                   "validation_mean_r": vm, "validation_trades": nval},
                      "fitted": fm, "test": test, "approved": appr, "probability_authorizes": fm.kind == "LOGISTIC_PROB",
                      "test_calibration": cal_rep})
    return {"ledger": ledger, "steps": steps}


def _ci_fail(res, name):
    if res.get("authority_status") != inf.AUTHORITY_VALID:
        return f"{name}_CI_NOT_ESTIMABLE"
    if not (res.get("authority_ci_low") or -1) > 0:
        return f"{name}_CI_NOT_POSITIVE"
    return None


def gate(steps, *, parity_ok: bool, arch_parity_ok: bool) -> dict:
    sel = [s["selected"] for s in steps]
    fails = []
    if not parity_ok:
        fails.append("RUNTIME_FEATURE_PARITY_FAILED")
    if not arch_parity_ok:
        fails.append("RUNTIME_ARCHITECTURE_PARITY_FAILED")
    if len(sel) != 2 or not all(sel):
        fails.append("ABSTAIN_IN_SOME_STEP")
    stable = {"direction": bool(all(sel) and len({s["side"] for s in sel}) == 1),
              "architecture": bool(all(sel) and len({(s["trigger"], s["family"]) for s in sel}) == 1)}
    if not stable["direction"]:
        fails.append("DIRECTION_UNSTABLE")
    if not stable["architecture"]:
        fails.append("ARCHITECTURE_UNSTABLE")
    test = sorted([t for s in steps for t in s["test"]], key=lambda r: r["ts"])
    ids = {id(t) for s in steps for t in s["approved"]}
    appr = [t for t in test if id(t) in ids]
    rep = {"stability": stable, "architecture_baseline": stats([t["r"] for t in test]),
           "pooled_approved": stats([t["r"] for t in appr])}
    if len(appr) < GATE["min_pooled_trades"]:
        fails.append("TOO_FEW_POOLED_TRADES")
    if not appr:
        return {**rep, "failures": sorted(set(fails + ["NO_APPROVED_TRADES"])), "all_pass": False,
                "label": EVIDENCE_LABEL}
    pooled = [dict(t, _a=id(t) in ids) for t in test]
    exp = inf.dependence_aware_mean(pooled, lambda r: r["_a"], required_ms=REQUIRED_MS)
    up = inf.dependence_aware_diff(pooled, lambda r: r["_a"], lambda r: True, required_ms=REQUIRED_MS)
    rep["expectancy"] = {k: exp.get(k) for k in ("mean_r", "n", "authority_status", "authority_ci_low",
                                                 "authority_ci_high", "iid_ci")}
    rep["uplift"] = {k: up.get(k) for k in ("delta", "authority_status", "authority_ci_low", "authority_ci_high",
                                            "iid_ci")}
    if not (exp.get("mean_r") or 0) > 0:
        fails.append("EXPECTANCY_NOT_POSITIVE")
    f = _ci_fail(exp, "EXPECTANCY")
    if f:
        fails.append(f)
    if not (up.get("delta") or 0) > 0:
        fails.append("UPLIFT_NOT_POSITIVE")
    f = _ci_fail(up, "UPLIFT")
    if f:
        fails.append(f)
    rep["cost_stress"] = {k: float(np.mean([t[k] for t in appr])) for k in STRESS}
    for k, v in rep["cost_stress"].items():
        if not v > 0:
            fails.append(f"COST_STRESS_FAILS_{k.upper()}")
    rs = [t["r"] for t in appr]
    rep["concentration"] = {k: share([t[k] for t in appr], rs)
                            for k in ("symbol", "month", "quarter", "regime", "week", "direction")
                            if k != "direction"}
    for k, lim, code in (("symbol", GATE["max_top_symbol_share"], "SINGLE_SYMBOL_DOMINATES"),
                         ("month", GATE["max_top_month_share"], "SINGLE_MONTH_DOMINATES"),
                         ("quarter", GATE["max_top_quarter_share"], "SINGLE_QUARTER_DOMINATES"),
                         ("regime", GATE["max_top_regime_share"], "SINGLE_REGIME_DOMINATES"),
                         ("week", GATE["max_top_week_share"], "SINGLE_EPISODE_DOMINATES")):
        sh = rep["concentration"][k]["top_share_of_positive_r"]
        if sh is None or sh > lim:
            fails.append(code)
    rep["max_drawdown_r"] = max_drawdown(appr)
    rep["avg_holding_h"] = float(np.mean([t["hold_h"] for t in appr]))
    rep["calibration"] = [s.get("test_calibration") for s in steps]
    for s in steps:
        c = s.get("test_calibration")
        if s.get("probability_authorizes") and (c is None or not c.get("beats_base_rate")
                                                 or (c.get("ece") or 1) > GATE["max_ece"]):
            fails.append("CALIBRATION_FAILURE_WITH_PROBABILITY_AUTHORITY")
    return {**rep, "failures": sorted(set(fails)), "all_pass": not fails, "label": EVIDENCE_LABEL}


SAMPLE_GATE_FAILURES = {"TOO_FEW_POOLED_TRADES", "EXPECTANCY_CI_NOT_ESTIMABLE", "UPLIFT_CI_NOT_ESTIMABLE"}


MIN_TESTABLE_FRACTION = 0.5


def testable_fraction(pre_step: list) -> float:
    with_trades = [p for p in pre_step if p["n"] > 0]
    return (sum(1 for p in with_trades if p["n"] >= PRE["min_trades"]) / len(with_trades)) if with_trades else 0.0


def classify(g: dict, pre_by_step: list) -> tuple[str, str]:
    if g["all_pass"]:
        return "CANDIDATE_SUPPORTED", "every supporting gate passes"
    fails = set(g["failures"])
    if "ABSTAIN_IN_SOME_STEP" not in fails:
        if fails <= SAMPLE_GATE_FAILURES and (g.get("pooled_approved") or {}).get("mean_r", -1) > 0:
            return "INSUFFICIENT_EVIDENCE", "only sample-size gate failures with positive point expectancy"
        return "NO_VALID_CHALLENGER", "economic / stability gate failures"
    fr = [testable_fraction(p) for p in pre_by_step]
    if min(fr) < MIN_TESTABLE_FRACTION:
        return "INSUFFICIENT_EVIDENCE", f"testable training fraction {fr} < {MIN_TESTABLE_FRACTION}"
    return "NO_VALID_CHALLENGER", f"no architecture passes the economic pre-gate in some step (testable fraction {fr})"


def max_drawdown(rows) -> float:
    eq = peak = dd = 0.0
    for r in sorted(rows, key=lambda r: (r["outcome_end_ts"], r["ts"])):
        eq += float(r["r"])
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    return dd


# ── portfolio (supporting research only) ───────────────────────────────────
def portfolio(trades, policy=PORTFOLIO) -> dict:
    """Fixed-fraction risk, concurrency / per-symbol / open-risk / notional caps."""
    eq = policy["start_equity"]
    peak, mdd = eq, 0.0
    open_pos, taken, skipped = [], [], defaultdict(int)
    conc_hist = defaultdict(int)
    notional_total, wins, losses = 0.0, 0.0, 0.0
    events_ = sorted(trades, key=lambda t: (t["ts"], t["symbol"]))
    exposure_ms, last_t = 0, None
    t_first = events_[0]["ts"] if events_ else 0

    def close_until(t):
        nonlocal eq, peak, mdd, wins, losses
        for p in sorted([p for p in open_pos if p["end_ts"] <= t], key=lambda p: p["end_ts"]):
            pnl = p["risk_amt"] * p["r"]
            eq += pnl
            wins += max(pnl, 0)
            losses += max(-pnl, 0)
            peak = max(peak, eq)
            mdd = max(mdd, (peak - eq) / peak if peak > 0 else 0.0)
            open_pos.remove(p)
    for t in events_:
        if last_t is not None and open_pos:
            exposure_ms += t["ts"] - last_t
        close_until(t["ts"])
        last_t = t["ts"]
        risk_amt = policy["risk_per_trade"] * eq
        notional = risk_amt / t["stop_frac"]
        open_risk = sum(p["risk_amt"] for p in open_pos) + risk_amt
        gross_notional = sum(p["notional"] for p in open_pos) + notional
        if len(open_pos) >= policy["max_concurrent"]:
            skipped["MAX_CONCURRENT"] += 1
            continue
        if sum(1 for p in open_pos if p["symbol"] == t["symbol"]) >= policy["max_per_symbol"]:
            skipped["MAX_PER_SYMBOL"] += 1
            continue
        if open_risk > policy["max_open_risk"] * eq + 1e-9:
            skipped["MAX_OPEN_RISK"] += 1
            continue
        if gross_notional > policy["max_gross_notional_x_equity"] * eq + 1e-9:
            skipped["MAX_NOTIONAL"] += 1
            continue
        open_pos.append({**t, "risk_amt": risk_amt, "notional": notional})
        taken.append(t)
        notional_total += notional
        conc_hist[len(open_pos)] += 1
    close_until(float("inf"))
    span_days = ((max(t["end_ts"] for t in events_) - t_first) / DAY) if events_ else 0
    return {"policy_sha256": PORTFOLIO_POLICY_SHA256, "trades": len(taken), "skipped": dict(skipped),
            "total_return": eq / policy["start_equity"] - 1.0, "max_drawdown": mdd,
            "profit_factor": (wins / losses) if losses > 0 else None,
            "exposure_fraction": (exposure_ms / (span_days * DAY)) if span_days else None,
            "turnover_notional_x_start_equity": notional_total / policy["start_equity"],
            "avg_holding_h": float(np.mean([t["hold_h"] for t in taken])) if taken else None,
            "concurrent_positions_at_entry": dict(sorted(conc_hist.items())), "span_days": span_days,
            "max_concurrent_seen": max(conc_hist) if conc_hist else 0,
            "note": "supporting research only; not proof of future performance"}


# ── runtime architecture parity ────────────────────────────────────────────
def runtime_event(c1h, decision_ts: int, family: str, side: str, trig: str, ts_index=None) -> bool:
    s = 1.0 if side == "LONG" else -1.0
    prev_ts = decision_ts - (H1 if trig == "h1" else lf.TF_MS["h4"])
    now = lf.runtime_raw(c1h, decision_ts, ts_index)
    before = lf.runtime_raw(c1h, prev_ts, ts_index)
    return bool(condition(family, now, s, trig)) and not bool(condition(family, before, s, trig))


def architecture_parity(c1h, F, *, per_signal: int = 6, runtime=runtime_event) -> dict:
    ts = F["ts"]
    ts_index = [int(t) for t in ts]
    checked = mism = 0
    stop_checked = stop_mism = 0
    examples = []
    rng = np.random.default_rng(7)
    for trig, fam, side in signals():
        s = 1.0 if side == "LONG" else -1.0
        ev = events(F, fam, s, trig)
        dec = decision_indices(ts, trig)
        dec = dec[(dec > lf.RUNTIME_1H_BARS + 8) & (dec > 0)]
        # an index is a decision only if the previous decision is exactly one period earlier
        period = H1 if trig == "h1" else lf.TF_MS["h4"]
        dec = np.array([i for i in dec if (ts[i] - period) in set(ts_index[max(0, i - 8):i])], dtype=int)
        if not len(dec):
            continue
        pos = dec[ev[dec]][:per_signal]
        neg = rng.choice(dec, size=min(per_signal, len(dec)), replace=False) if len(dec) else []
        for i in sorted(set(int(x) for x in list(pos) + list(neg))):
            rt = runtime(c1h, int(ts[i]), fam, side, trig, ts_index)
            checked += 1
            if rt != bool(ev[i]):
                mism += 1
                if len(examples) < 10:
                    examples.append({"i": i, "signal": f"{trig}|{fam}|{side}", "research": bool(ev[i]), "runtime": rt})
            if bool(ev[i]):
                raw = lf.runtime_raw(c1h, int(ts[i]), ts_index)
                e = float(c1h[i]["o"])
                for st in STOPS:
                    a = stop_distance(st, F, i, s, e)
                    b = stop_distance(st, {k: np.array([raw[k]]) for k in raw}, 0, s, e)
                    stop_checked += 1
                    if (a is None) != (b is None) or (a is not None and abs(a - b) > 1e-9 * max(1.0, abs(a))):
                        stop_mism += 1
    return {"events_checked": checked, "event_mismatches": mism, "stops_checked": stop_checked,
            "stop_mismatches": stop_mism, "examples": examples,
            "parity": checked > 0 and mism == 0 and stop_mism == 0}


# ── freeze / export ────────────────────────────────────────────────────────
PROBE = [[round(math.sin(i * 7 + k) * 2, 6) for k in range(len(lf.MODEL_FEATURES))] for i in range(16)]


def _canon(o) -> bytes:
    return json.dumps(o, sort_keys=True, separators=(",", ":")).encode()


def freeze(result, *, dataset_sha: str, code_sha: str, created_at: str) -> dict:
    if result.get("result") != "CANDIDATE_SUPPORTED":
        raise ValueError("no supported candidate; refusing to freeze")
    st = result["steps"][-1]
    sel, fm = st["selected"], st["fitted"]
    arch = {"trigger": sel["trigger"], "family": sel["family"], "side": sel["side"], "entry": sel["entry"],
            "stop": sel["stop"], "exit": sel["exit"], "family_rule": SPEC["families"][sel["family"]],
            "context": list(CONTEXT[sel["trigger"]]), "entry_rule": SPEC["entries"][sel["entry"]],
            "stop_rule": SPEC["stops"][sel["stop"]], "exit_rule": SPEC["exits"][sel["exit"]]}
    policy = {"candidate_id": sel["candidate_id"], "model_kind": fm.kind, "threshold": fm.threshold,
              "probability_authorizes": fm.kind == "LOGISTIC_PROB", "exchange_credentials": None,
              "execution_lease": False, "order_authority": False, "live_authority": False}
    model = fm.to_json()
    manifest = {"schema": "LONG_HORIZON_ARCHITECTURE_BUNDLE_V1",
                "runtime_compatibility": "NOT AI_MODEL_BUNDLE_V1; not loadable by the Phase-7/8 observer",
                "lifecycle_state": "SHADOW_CHALLENGER", "evidence_label": EVIDENCE_LABEL,
                "search_spec_sha256": SPEC_SHA256, "cost_policy_sha256": COST_POLICY_SHA256,
                "exit_policy_sha256": EXIT_POLICY_SHA256, "portfolio_policy_sha256": PORTFOLIO_POLICY_SHA256,
                "feature_schema": lf.VERSION, "feature_schema_sha256": lf.SCHEMA_SHA256,
                "training_dataset_sha256": dataset_sha, "training_code_sha": code_sha, "created_at": created_at,
                "architecture_sha256": hashlib.sha256(_canon(arch)).hexdigest(),
                "policy_sha256": hashlib.sha256(_canon(policy)).hexdigest(),
                "model_sha256": hashlib.sha256(_canon(model)).hexdigest(),
                "probe_scores": [float(v) for v in fm.score(np.array(PROBE))]}
    manifest["bundle_sha256"] = hashlib.sha256(_canon(manifest)).hexdigest()
    return {"architecture": arch, "policy": policy, "model": model, "manifest": manifest}


def export(bundle, out_dir) -> dict:
    from pathlib import Path
    p = Path(out_dir)
    p.mkdir(parents=True, exist_ok=True)
    shas = {}
    for name in ("architecture", "policy", "model", "manifest"):
        b = _canon(bundle[name])
        (p / f"{name}.json").write_bytes(b)
        shas[f"{name}.json"] = hashlib.sha256(b).hexdigest()
    return {"file_sha256": shas}


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
    fm = FittedModel.from_json(b["model"])
    s1, s2 = fm.score(np.array(PROBE)), fm.score(np.array(PROBE))
    det = bool(np.array_equal(s1, s2)) and np.allclose(s1, m["probe_scores"], rtol=0, atol=1e-12)
    if not det:
        fails.append("NON_DETERMINISTIC_INFERENCE")
    return {"verified": not fails, "failures": fails, "bundle_sha256": bsha, "lifecycle_state": m["lifecycle_state"],
            "schema": m["schema"], "deterministic_inference": det}


# ── orchestration ──────────────────────────────────────────────────────────
DIMENSIONS = ("trigger", "family", "side", "entry", "stop", "exit")


class Acc:
    """Streaming diagnostic accumulator keyed by (dimension, value)."""

    def __init__(self):
        self.d = defaultdict(lambda: defaultdict(float))

    def add(self, key, b):
        a = self.d[key]
        r, g = b["r"], b["gross_r"]
        tot = b["fee_r"] + b["slip_r"] + b["fund_r"] + UNCERTAINTY_BUFFER_R
        for name, v in (("n", len(r)), ("sum_r", r.sum()), ("sum_gross", g.sum()), ("sum_win", r[r > 0].sum()),
                        ("sum_loss", -r[r < 0].sum()), ("wins", (r > 0).sum()), ("sum_cost", tot.sum()),
                        ("sum_fee", b["fee_r"].sum()), ("sum_slip", b["slip_r"].sum()), ("sum_fund", b["fund_r"].sum()),
                        ("sum_abs_gross", np.abs(g).sum()), ("sum_stop_frac", b["stop_frac"].sum()),
                        ("sum_hold", b["hold_h"].sum()), ("sum_mfe", b["mfe_r"].sum()), ("sum_mae", b["mae_r"].sum()),
                        ("stop_first", b["stop_first"].sum()), ("plus1_before_stop", b["plus1_before_stop"].sum()),
                        ("sum_settle", b["n_settle"].sum()), ("sum_combined", b["combined_adverse"].sum()),
                        *[(f"sum_abs_move_{H}h", b[f"abs_move_r_{H}h"].sum()) for H in HOLDING_HORIZONS_H]):
            a[name] += float(v)

    def report(self, dim):
        out = {}
        for (d, v), a in sorted(self.d.items()):
            if d != dim or not a["n"]:
                continue
            n = a["n"]
            out[v] = {"n_trades_across_tuples": int(n), "mean_net_r": a["sum_r"] / n, "mean_gross_r": a["sum_gross"] / n,
                      "win_rate": a["wins"] / n, "profit_factor": (a["sum_win"] / a["sum_loss"]) if a["sum_loss"] else None,
                      "mean_combined_adverse_r": a["sum_combined"] / n,
                      "fee_r": a["sum_fee"] / n, "slippage_r": a["sum_slip"] / n, "funding_r": a["sum_fund"] / n,
                      "total_cost_r": a["sum_cost"] / n, "gross_edge_to_cost": (a["sum_gross"] / a["sum_cost"]),
                      "abs_move_to_cost": a["sum_abs_gross"] / a["sum_cost"], "mean_stop_pct": 100 * a["sum_stop_frac"] / n,
                      "mean_holding_h": a["sum_hold"] / n, "mean_mfe_r": a["sum_mfe"] / n, "mean_mae_r": a["sum_mae"] / n,
                      "stop_first_rate": a["stop_first"] / n, "plus1r_before_stop_rate": a["plus1_before_stop"] / n,
                      "mean_settlements": a["sum_settle"] / n,
                      **{f"abs_move_{H}h_to_cost": a[f"sum_abs_move_{H}h"] / a["sum_cost"] for H in HOLDING_HORIZONS_H}}
        return out


def run(data: dict, window: dict, *, parity_ok: bool, arch_parity_ok: bool, sym_ctx=None, progress=None) -> dict:
    span = (window["decision_start_ms"], window["decision_end_ms"])
    lay = fold_layout(*span)
    if lay["status"] != "OK":
        return {"status": "INSUFFICIENT_INDEPENDENT_FOLDS", "result": "INSUFFICIENT_EVIDENCE", "layout": lay}
    sym_ctx = sym_ctx or symbol_context(data)
    acc = Acc()
    baselines, ledger_raw, kept = {}, [], {}
    pre_by_step = [[], []]
    n_trades = 0
    tsha = hashlib.sha256()
    for trig, fam, side in signals():
        blocks = generate_signal(sym_ctx, trig, fam, side, span=span)
        for tid in sorted(blocks):
            b = blocks[tid]
            n_trades += len(b["r"])
            tsha.update(tid.encode())
            tsha.update(np.round(b["r"], 10).tobytes())
            b["fold"] = fold_of(b, lay)
            parts = dict(zip(DIMENSIONS, tid.split("|")))
            for dim, v in parts.items():
                acc.add((dim, v), b)
            if parts["exit"].startswith("TIME_"):
                acc.add(("holding_horizon", parts["exit"]), b)
            baselines[tid] = {**stats(b["r"]), "gross": stats(b["gross_r"]), **cost_diag(b),
                              "combined_adverse_mean_r": float(b["combined_adverse"].mean()),
                              "mean_mfe_r": float(b["mfe_r"].mean()), "mean_mae_r": float(b["mae_r"].mean()),
                              "mean_holding_h": float(b["hold_h"].mean()),
                              "by_fold": {str(k + 1): stats(b["r"][b["fold"] == k]).get("mean_r") for k in range(4)}}
            pres = []
            for si, (tr_idx, va, ev) in enumerate(STEPS):
                mt = np.isin(b["fold"], tr_idx)
                pg = pre_gate(b, mt)
                pres.append(pg)
                pre_by_step[si].append(pg)
                ledger_raw.append({"step": si + 1, "tuple": tid, "candidate_id": f"{tid}|RAW", "model": "RAW",
                                   "promotable": False, "search_spec_sha256": SPEC_SHA256, "pre_gate": pg,
                                   "raw_validation": stats(b["r"][b["fold"] == va]),
                                   "raw_outer_posthoc": stats(b["r"][b["fold"] == ev])})
            if any(p["pass"] for p in pres):
                b["pre"] = pres
                kept[tid] = b
        if progress:
            progress(f"{trig}|{fam}|{side}: {len(blocks)} tuples, kept so far {len(kept)}")
    s = search(kept, ledger_raw)
    g = gate(s["steps"], parity_ok=parity_ok, arch_parity_ok=arch_parity_ok)
    result, why = classify(g, pre_by_step)
    promo = [e for e in s["ledger"] if e.get("promotable")
             and (e.get("outer_posthoc_not_used_for_selection") or {}).get("n", 0) >= MIN_VALIDATION_TRADES]
    raw = [e for e in s["ledger"] if not e.get("promotable") and e["raw_outer_posthoc"].get("n", 0) >= MIN_VALIDATION_TRADES]
    bp = max(promo, key=lambda e: e["outer_posthoc_not_used_for_selection"]["mean_r"], default=None)
    br = max(raw, key=lambda e: e["raw_outer_posthoc"]["mean_r"], default=None)
    appr = [t for st in s["steps"] for t in st["approved"]]
    W_ = lay["windows"]
    days = sum((w["decision_end_ts"] - w["decision_start_ts"]) / DAY for w in W_[2:])
    return {"status": "OK", "result": result, "classification_reason": why, "gate": g, "ledger": s["ledger"],
            "steps": s["steps"], "trades": n_trades, "trades_sha256": tsha.hexdigest(),
            "architectures": architecture_count(), "tuples_with_trades": len(baselines),
            "pregate_pass_counts": {str(k + 1): sum(1 for p in pre_by_step[k] if p["pass"]) for k in range(2)},
            "pregate_only_sample_failures": {str(k + 1): sum(1 for p in pre_by_step[k] if p["only_sample_failures"])
                                             for k in range(2)},
            "testable_training_fraction": {str(k + 1): testable_fraction(pre_by_step[k]) for k in range(2)},
            "baselines": baselines, "acc": acc,
            "best_posthoc_outer": {
                "model_candidate": None if bp is None else {"candidate_id": bp["candidate_id"], "step": bp["step"],
                                                            **bp["outer_posthoc_not_used_for_selection"]},
                "raw_architecture": None if br is None else {"candidate_id": br["candidate_id"], "step": br["step"],
                                                             **br["raw_outer_posthoc"]},
                "note": "data-snooping diagnostic only; never used for selection or freeze; cannot be promoted"},
            "portfolio": portfolio(appr) if appr else {"status": "NOT_RUN_NO_APPROVED_TRADES"},
            "estimate": {"outer_eval_days": days, "approved": len(appr),
                         "estimated_trades_per_72h": len(appr) / days * 3 if days else None},
            "layout": {"status": lay["status"], "windows": W_}}


def public_steps(steps):
    return [{k: v for k, v in st.items() if k not in ("test", "approved", "fitted")}
            | {"outer_eval": {"architecture_baseline": stats([t["r"] for t in st["test"]]),
                              "approved": stats([t["r"] for t in st["approved"]])}} for st in steps]


def main(argv=None) -> int:
    import argparse
    from pathlib import Path
    ap = argparse.ArgumentParser(description="Phase 8F long-horizon architecture research (research only)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("--out", required=True)
    r = sub.add_parser("run")
    r.add_argument("--dataset", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--candidate-sha", required=True)
    r.add_argument("--created-at", required=True)
    a = ap.parse_args(argv)
    if a.cmd == "fetch":
        data, meta = asyncio.run(fetch_dataset())
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        sha = save_dataset(data, a.out, meta)
        print(json.dumps({"raw_dataset_sha256": sha, "bars": {s: len(v) for s, v in data.items()},
                          "pagination": meta}, sort_keys=True))
        return 0
    raw, header = load_dataset(a.dataset)
    check_no_forward_evidence(raw)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    def dump(name, obj):
        (out / name).write_text(json.dumps(obj, indent=1, sort_keys=True, default=str) + "\n")
    dump("long_horizon_spec.json", {"spec": SPEC, "spec_sha256": SPEC_SHA256, "cost_policy_sha256": COST_POLICY_SHA256,
                                    "exit_policy_sha256": EXIT_POLICY_SHA256,
                                    "portfolio_policy_sha256": PORTFOLIO_POLICY_SHA256,
                                    "feature_schema": lf.SCHEMA, "feature_schema_sha256": lf.SCHEMA_SHA256})
    win = select_window(raw)
    lo = (win.get("decision_start_ms", 0) - WARMUP_DAYS * DAY)
    data = {s: [b for b in raw[s] if lo <= b["ts"] < DECISION_END_MS + 3 * DAY] for s in win.get("symbols", [])}
    dsha = dataset_sha256(data) if data else None
    cov = {"raw_dataset_sha256": header["sha256"], "dataset_sha256": dsha, "pagination": header.get("meta"),
           "window": {k: v for k, v in win.items() if k != "candidates"},
           "candidates": [{"days": c["days"], "passing_symbols": c["passing_symbols"],
                           "coverage": c["coverage"]} for c in win["candidates"]]}
    if data:
        cov["counts"] = {s: {"h1": len(v), "h4": len(lf.aggregate(v, "h4")), "d1": len(lf.aggregate(v, "d1"))}
                         for s, v in data.items()}
        cov["data_start_ms"], cov["data_end_ms"] = win["decision_start_ms"], win["decision_end_ms"]
        cov["data_days"] = win["days"]
    dump("dataset_coverage.json", cov)
    summary = {"version": VERSION, "evidence_label": EVIDENCE_LABEL, "spec_sha256": SPEC_SHA256,
               "cost_policy_sha256": COST_POLICY_SHA256, "exit_policy_sha256": EXIT_POLICY_SHA256,
               "portfolio_policy_sha256": PORTFOLIO_POLICY_SHA256, "feature_schema_sha256": lf.SCHEMA_SHA256,
               "raw_dataset_sha256": header["sha256"], "dataset_sha256": dsha, "candidate_code_sha": a.candidate_sha,
               "window": cov["window"]}
    if win["status"] != "OK":
        summary.update({"status": "INSUFFICIENT_COVERAGE", "result": "INSUFFICIENT_EVIDENCE"})
        dump("phase8f_summary.json", summary)
        print(json.dumps(summary, indent=1, default=str))
        return 0
    sym_ctx = symbol_context(data)
    fp = {s: lf.parity_report(data[s], F=sym_ctx[s]["F"]) for s in sorted(data)}
    ap_ = {s: architecture_parity(data[s], sym_ctx[s]["F"]) for s in sorted(data)}
    parity_ok = all(p["parity"] for p in fp.values())
    arch_ok = all(p["parity"] for p in ap_.values())
    dump("feature_parity_report.json", {"feature_parity": parity_ok, "architecture_parity": arch_ok,
                                        "per_symbol_features": fp, "per_symbol_architecture": ap_})
    res = run(data, win, parity_ok=parity_ok, arch_parity_ok=arch_ok, sym_ctx=sym_ctx,
              progress=lambda m: print(m, flush=True))
    acc = res["acc"]
    dump("raw_architecture_baselines.json", {"role": "RAW_BEFORE_ML", "label": EVIDENCE_LABEL,
                                             "tuples": res["baselines"]})
    dump("timeframe_analysis.json", {"by_trigger": acc.report("trigger"), "by_family": acc.report("family"),
                                     "by_side": acc.report("side")})
    dump("holding_horizon_analysis.json", {"by_time_exit": acc.report("holding_horizon"),
                                           "by_exit": acc.report("exit")})
    dump("cost_to_stop_analysis.json", {"by_stop": acc.report("stop"), "by_trigger": acc.report("trigger"),
                                        "by_exit": acc.report("exit")})
    dump("entry_analysis.json", {"by_entry": acc.report("entry")})
    dump("stop_analysis.json", {"by_stop": acc.report("stop")})
    dump("exit_analysis.json", {"by_exit": acc.report("exit")})
    dump("portfolio_analysis.json", res["portfolio"])
    with gzip.open(out / "architecture_ledger.json.gz", "wt", encoding="utf-8") as fh:
        fh.write(json.dumps(res["ledger"], sort_keys=True, default=str))
    summary.update({k: res.get(k) for k in ("status", "result", "classification_reason", "trades", "trades_sha256",
                                            "architectures", "tuples_with_trades", "pregate_pass_counts",
                                            "pregate_only_sample_failures", "testable_training_fraction", "best_posthoc_outer", "estimate",
                                            "gate", "layout")})
    summary.update({"ledger_entries": len(res["ledger"]), "steps": public_steps(res["steps"]),
                    "feature_parity": parity_ok, "architecture_parity": arch_ok,
                    "portfolio": res["portfolio"]})
    if res["result"] == "CANDIDATE_SUPPORTED":
        b = freeze(res, dataset_sha=dsha, code_sha=a.candidate_sha, created_at=a.created_at)
        exp = export(b, out / "challenger_bundle")
        v1, v2 = verify(out / "challenger_bundle"), verify(out / "challenger_bundle")
        if not (v1["verified"] and v1 == v2):
            import shutil
            shutil.rmtree(out / "challenger_bundle")
            summary["result"] = "NO_VALID_CHALLENGER"
            summary["gate"]["failures"] = sorted(set(summary["gate"]["failures"] + ["BUNDLE_NOT_DETERMINISTIC"]))
        else:
            summary["frozen"] = {"manifest": b["manifest"], "file_sha256": exp["file_sha256"], "verify": v1}
    dump("phase8f_summary.json", summary)
    print(json.dumps({k: summary.get(k) for k in ("result", "classification_reason", "trades", "dataset_sha256",
                                                  "spec_sha256", "feature_parity", "architecture_parity",
                                                  "pregate_pass_counts", "pregate_only_sample_failures",
                                                  "estimate", "best_posthoc_outer")},
                     indent=1, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
