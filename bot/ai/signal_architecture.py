"""Phase 8E: SIGNAL_ARCHITECTURE_SPEC_V1 - candidate-generator redesign (research only).

The NEXUS candidate population is NOT assumed correct. A bounded, predeclared
family of architectures

    setup (explicit direction from market structure) x entry trigger x stop geometry x side

is generated from closed 15m/1h/4h candles only (bot.ai.arch_features, with a
runtime-path parity check), simulated with ONE fixed exit (stop, +2R target,
8h time exit), costed net of fees + slippage + funding allowance + uncertainty
buffer, pre-gated on TRAINING folds only, then filtered by a small model
family inside a nested chronological walk-forward. A challenger is frozen
only if every predeclared gate passes. Every tested tuple is ledgered.
Evidence label: PREVIOUSLY_INSPECTED_RESEARCH_SUPPORT (never UNTOUCHED_OOS).

Capital at risk is fixed at 1R per trade: notional = risk / stop_frac, so a
wider stop always means a SMALLER position; costs scale with notional and are
charged in R as cost_frac / stop_frac. No result can improve by adding risk.
"""
from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import json
import math
from collections import defaultdict

import numpy as np

from bot import nexus_oos_inference as inf
from bot.ai import arch_features as af
from bot.ai import calibration as cal
from bot.ai import models as mdl

EVIDENCE_LABEL = "PREVIOUSLY_INSPECTED_RESEARCH_SUPPORT"
VERSION = "SIGNAL_ARCHITECTURE_SPEC_V1"
FORWARD_EVIDENCE_CUTOFF_MS = 1_790_000_000_000          # identical to Phase 8D; the Phase-8 window never opened
BAR_MS = af.BAR15_MS

# data window: the SAME decision span as the Phase 8D dataset (4f164705...)
DECISION_START_MS = 1_772_208_900_000                   # 2026-02-27T16:15Z
DECISION_END_MS = 1_787_759_100_000 + BAR_MS            # 2026-08-26 (inclusive last Phase-8D decision)
SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT", "DOGEUSDT", "LINKUSDT", "AVAXUSDT", "DOTUSDT",
           "LTCUSDT", "NEARUSDT", "ATOMUSDT")
MAJORS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")

SETUPS = ("TREND_PULLBACK", "BREAKOUT_CONT", "RANGE_MEANREV", "FAILED_BREAKOUT", "VOL_EXPANSION",
          "HTF_ALIGN_MOMENTUM")
ENTRIES = ("IMMEDIATE", "CONFIRM_1", "CONFIRM_2", "RETEST")
STOPS = ("ATR_1_0", "ATR_1_5", "ATR_2_0", "SWING_8", "VOL_STRUCT")
SIDES = ("LONG", "SHORT")
TARGET_R = 2.0
MAX_HOLD_BARS = 32                                      # 8h time exit at the close of the 32nd bar
RETEST_BARS = 4
MAX_STOP_FRAC = 0.08

# cost policy (replay manifest values; per side)
TAKER_FEE = 0.0006
SLIPPAGE_MAJOR, SLIPPAGE_ALT = 0.0005, 0.0010
FUNDING_ALLOWANCE = 0.0001                              # one adverse 0.01% settlement per trade (<= 8h hold)
UNCERTAINTY_BUFFER_R = 0.05                             # DecisionPolicy.uncertainty_buffer_r default
MAX_RISK_PCT, LEVERAGE = 0.01, 50
MIN_STOP_FRAC = MAX_RISK_PCT / LEVERAGE                 # notional cap: risk/stop_frac <= leverage x equity

# pre-gate (TRAINING folds only)
PRE_MIN_TRADES = 60
PRE_MIN_MONTHS = 2
PRE_MIN_MONTH_TRADES = 10
PRE_MIN_SYMBOLS = 3
# model family / selection
MODELS = (("RIDGE_NET", {"l2": 10.0}, (0.0, 0.05, 0.10)), ("LOGISTIC_PROB", {"l2": 1.0}, (0.50, 0.55, 0.60)))
MIN_VALIDATION_TRADES = 30
# supporting gate
MIN_POOLED_TRADES = 60
MAX_TOP_SYMBOL_SHARE, MAX_TOP_MONTH_SHARE, MAX_TOP_REGIME_SHARE = 0.5, 0.5, 0.6
MAX_ECE = 0.10
REQUIRED_COST_SCENARIOS = ("fees_plus_50pct", "slippage_x2", "combined_adverse")
REQUIRED_MS = inf.DAY_MS

SPEC = {
    "version": VERSION, "evidence_label": EVIDENCE_LABEL, "forward_evidence_cutoff_ms": FORWARD_EVIDENCE_CUTOFF_MS,
    "data": {"symbols": list(SYMBOLS), "decision_start_ms": DECISION_START_MS, "decision_end_ms": DECISION_END_MS,
             "source": "KuCoin Futures public klines 15m/1h/4h", "decision_time": "open of 15m bar i; bars < i"},
    "features": {"schema": af.VERSION, "schema_sha256": af.SCHEMA_SHA256},
    "setups": {
        "TREND_PULLBACK": "trend4h==s & trend1h==s & s*(c-sma20)>0 & (LONG lo8<=sma20 | SHORT hi8>=sma20)",
        "BREAKOUT_CONT": "trend1h==s & (LONG c>donhi32 | SHORT c<donlo32)",
        "RANGE_MEANREV": "range4h & s*(c-sma20)/std20 < -2",
        "FAILED_BREAKOUT": "LONG l_last<donlo32 & c>donlo32 | SHORT h_last>donhi32 & c<donhi32",
        "VOL_EXPANSION": "prior_atr_rank96<=0.2 & range_ratio>=2 & body==s",
        "HTF_ALIGN_MOMENTUM": "trend4h==s & trend1h==s & s*(sma20-sma50)>0 & s*roc16>0",
        "event": "condition true at decision i and false at decision i-1"},
    "entries": {"IMMEDIATE": "market at open of i",
                "CONFIRM_1": "bar i closes in direction -> market at open of i+1, else cancel",
                "CONFIRM_2": "bars i and i+1 close in direction -> market at open of i+2, else cancel",
                "RETEST": "level=c[i-1]-s*0.5*atr14; first bar k in i..i+3 touching level and closing beyond it "
                          "in direction -> market at open of k+1, else cancel (no limit-fill assumption)"},
    "stops": {"ATR_1_0": "1.0*atr14", "ATR_1_5": "1.5*atr14", "ATR_2_0": "2.0*atr14",
              "SWING_8": "beyond 8-bar swing extreme + 0.1*atr14; min 0.5*atr14",
              "VOL_STRUCT": "max(beyond 8-bar swing + 0.25*atr14, 1.0*atr14)",
              "feasibility": f"{MIN_STOP_FRAC} <= stop_frac <= {MAX_STOP_FRAC}",
              "sizing": "fixed 1R capital at risk; notional = risk/stop_frac (wider stop => smaller size)"},
    "exit": {"target_r": TARGET_R, "max_hold_bars_15m": MAX_HOLD_BARS,
             "intrabar": "stop before target in the same bar; gap through stop fills at the open",
             "overlap": "one open trade per (symbol, setup, entry, stop, side)"},
    "costs": {"taker_fee_per_side": TAKER_FEE, "slippage_per_side": {"majors": SLIPPAGE_MAJOR, "alts": SLIPPAGE_ALT},
              "funding_allowance_fraction": FUNDING_ALLOWANCE, "uncertainty_buffer_r": UNCERTAINTY_BUFFER_R,
              "stress": {"fees_plus_50pct": "fees x1.5", "slippage_x2": "slippage x2",
                         "combined_adverse": "fees x1.5, slippage x2, funding x2"}},
    "pre_gate_training_only": {"min_trades": PRE_MIN_TRADES, "net_mean_gt_0": True, "combined_adverse_mean_gt_0": True,
                               "min_months_with_10_trades": PRE_MIN_MONTHS, "half_of_those_months_positive": True,
                               "min_symbols": PRE_MIN_SYMBOLS, "max_top_symbol_share": MAX_TOP_SYMBOL_SHARE,
                               "max_top_regime_share": MAX_TOP_REGIME_SHARE,
                               "month": "'only one month' = the months rule above (month share is a final gate: "
                                        "a 1.5-month training fold cannot satisfy a 50% share rule)",
                               "direction": "LONG and SHORT are separate hypotheses; a side without evidence is "
                                            "disabled; direction stability is a supporting gate"},
    "models": [[m, hp, list(th)] for m, hp, th in MODELS],
    "model_rules": {"RIDGE_NET": "ridge on realized NET R; approve predicted net R > threshold",
                    "LOGISTIC_PROB": "logistic on NET R>0, Platt on validation; authorizes ONLY if validation "
                                     "calibration beats base-rate Brier; approve p >= threshold",
                    "RAW": "architecture baseline (reported, never promotable: uplift vs itself is 0)"},
    "walk_forward": {"folds": 4, "required_horizon_ms": REQUIRED_MS,
                     "outer_steps": [{"train": [1], "validate": 2, "evaluate": 3},
                                     {"train": [1, 2], "validate": 3, "evaluate": 4}],
                     "selection": "max validation mean NET R with >= 30 approved validation trades and mean > 0"},
    "supporting_gate": {"non_abstain_both_steps": True, "min_pooled_trades": MIN_POOLED_TRADES,
                        "expectancy_gt_0_and_block_ci_low_gt_0": True,
                        "uplift_vs_architecture_baseline_gt_0_and_block_ci_low_gt_0": True,
                        "cost_stress_positive": list(REQUIRED_COST_SCENARIOS),
                        "max_top_symbol_share": MAX_TOP_SYMBOL_SHARE, "max_top_month_share": MAX_TOP_MONTH_SHARE,
                        "max_top_regime_share": MAX_TOP_REGIME_SHARE, "stable_direction": True,
                        "stable_setup": True, "runtime_feature_parity": True,
                        "calibration_if_probability_authorizes": {"beats_base_rate": True, "max_ece": MAX_ECE},
                        "deterministic_export_load_inference": True},
}
SPEC_SHA256 = hashlib.sha256(json.dumps(SPEC, sort_keys=True).encode()).hexdigest()
COST_POLICY = SPEC["costs"]
COST_POLICY_SHA256 = hashlib.sha256(json.dumps(COST_POLICY, sort_keys=True).encode()).hexdigest()
EXIT_POLICY = SPEC["exit"]
EXIT_POLICY_SHA256 = hashlib.sha256(json.dumps(EXIT_POLICY, sort_keys=True).encode()).hexdigest()


class ForwardEvidenceRefused(ValueError):
    pass


# ── data ───────────────────────────────────────────────────────────────────
def dataset_sha256(data: dict) -> str:
    canon = {s: {tf: [[int(b["ts"]), float(b["o"]), float(b["h"]), float(b["l"]), float(b["c"]), float(b["v"])]
                      for b in data[s][tf]] for tf in ("15", "60", "240")} for s in sorted(data)}
    return hashlib.sha256(json.dumps(canon, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def save_dataset(data: dict, path) -> str:
    sha = dataset_sha256(data)
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        json.dump({"header": {"version": VERSION, "sha256": sha, "symbols": sorted(data)},
                   "data": {s: {tf: [[int(b["ts"]), b["o"], b["h"], b["l"], b["c"], b["v"]] for b in data[s][tf]]
                                for tf in ("15", "60", "240")} for s in sorted(data)}}, fh, sort_keys=True)
    return sha


def load_dataset(path) -> tuple[dict, dict]:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        raw = json.load(fh)
    data = {s: {tf: [{"ts": int(b[0]), "o": float(b[1]), "h": float(b[2]), "l": float(b[3]), "c": float(b[4]),
                      "v": float(b[5])} for b in raw["data"][s][tf]] for tf in ("15", "60", "240")}
            for s in raw["data"]}
    if dataset_sha256(data) != raw["header"]["sha256"]:
        raise ValueError("dataset hash mismatch")
    return data, raw["header"]


def check_no_forward_evidence(data: dict):
    for s, tfs in data.items():
        for tf, bars in tfs.items():
            if bars and int(bars[-1]["ts"]) >= FORWARD_EVIDENCE_CUTOFF_MS:
                raise ForwardEvidenceRefused(f"{s} {tf}: candles at/after the forward-evidence cutoff")


FETCH_PAGE_BARS = 150            # KuCoin returns at most 200 klines per request; stay well below
MIN_COVERAGE = 0.99


def coverage(bars, start: int, end: int, step: int) -> dict:
    """Fail closed on silently missing history (pagination or exchange gaps)."""
    expected = max(1, (end - start) // step)
    ts = [int(b["ts"]) for b in bars]
    max_gap = max((b - a for a, b in zip(ts, ts[1:])), default=0) // step
    frac = len(ts) / expected
    return {"bars": len(ts), "expected": expected, "fraction": frac, "max_gap_bars": int(max_gap),
            "first_ts": ts[0] if ts else None, "last_ts": ts[-1] if ts else None,
            "ok": bool(ts) and frac >= MIN_COVERAGE and max_gap <= 8
            and ts[0] - start <= 8 * step and end - ts[-1] <= 9 * step}


async def fetch_dataset(symbols=SYMBOLS) -> dict:
    """Public KuCoin klines for the pinned window (+warm-up, +exit tail)."""
    from bot.backtest import _historical_integrity, _kucoin_page
    from bot.nexus_oos_real_replay import PublicKuCoinFuturesClient
    import asyncio
    warm = {"15": af.W15 + 8, "60": af.W1H + 4, "240": af.W4H + 4}
    step = {"15": BAR_MS, "60": af.BAR1H_MS, "240": af.BAR4H_MS}
    end = min(DECISION_END_MS + (MAX_HOLD_BARS + RETEST_BARS + 4) * BAR_MS, FORWARD_EVIDENCE_CUTOFF_MS)
    out = {}
    async with PublicKuCoinFuturesClient() as client:
        for s in symbols:
            out[s] = {}
            for tf in ("15", "60", "240"):
                start = DECISION_START_MS - warm[tf] * step[tf] - step[tf]
                cur, by = start, {}
                while cur < end:
                    to = min(end, cur + FETCH_PAGE_BARS * step[tf])
                    page = await _kucoin_page(client, s, tf, cur, to)
                    for b in page:
                        by[int(b["ts"])] = b
                    cur = to
                    await asyncio.sleep(0.05)
                bars = [by[t] for t in sorted(by) if start <= t and t + step[tf] <= end]
                integ = _historical_integrity(bars, tf)
                cov = coverage(bars, start, end, step[tf])
                if not integ["ok"] or not cov["ok"]:
                    raise RuntimeError(f"{s} {tf} integrity/coverage failed: {integ} {cov}")
                out[s][tf] = [{k: (int(b[k]) if k == "ts" else float(b[k])) for k in ("ts", "o", "h", "l", "c", "v")}
                              for b in bars]
    return out


# ── setups / entries / stops / simulation ──────────────────────────────────
def setup_condition(name: str, F: dict, s: float):
    """Elementwise on arrays (research) or scalars (runtime dict). NaN -> False."""
    with np.errstate(invalid="ignore", divide="ignore"):
        g = {k: np.asarray(F[k], float) for k in af.RAW_KEYS}
        if name == "TREND_PULLBACK":
            touch = (g["lo8"] <= g["sma20"]) if s > 0 else (g["hi8"] >= g["sma20"])
            out = (g["trend4h"] == s) & (g["trend1h"] == s) & (s * (g["c"] - g["sma20"]) > 0) & touch
        elif name == "BREAKOUT_CONT":
            out = (g["trend1h"] == s) & ((g["c"] > g["donhi32"]) if s > 0 else (g["c"] < g["donlo32"]))
        elif name == "RANGE_MEANREV":
            z = np.where(g["std20"] > 0, (g["c"] - g["sma20"]) / np.where(g["std20"] > 0, g["std20"], 1.0), np.nan)
            out = (g["range4h"] == 1.0) & (s * z < -2.0)
        elif name == "FAILED_BREAKOUT":
            out = (((g["l_last"] < g["donlo32"]) & (g["c"] > g["donlo32"])) if s > 0
                   else ((g["h_last"] > g["donhi32"]) & (g["c"] < g["donhi32"])))
        elif name == "VOL_EXPANSION":
            out = (g["prior_atr_rank96"] <= 0.2) & (g["range_ratio"] >= 2.0) & (g["body"] == s)
        elif name == "HTF_ALIGN_MOMENTUM":
            out = ((g["trend4h"] == s) & (g["trend1h"] == s) & (s * (g["sma20"] - g["sma50"]) > 0)
                   & (s * g["roc16"] > 0))
        else:
            raise ValueError(name)
    return out


def events(F: dict, name: str, s: float) -> np.ndarray:
    c = np.asarray(setup_condition(name, F, s), bool)
    ev = np.zeros(len(c), bool)
    ev[1:] = c[1:] & ~c[:-1]
    return ev


def entry_index(arr, i: int, entry: str, s: float, atr: float):
    o, h, lo, c = arr
    n = len(o)
    if entry == "IMMEDIATE":
        return i
    if entry == "CONFIRM_1":
        return i + 1 if i < n and s * (c[i] - o[i]) > 0 else None
    if entry == "CONFIRM_2":
        return i + 2 if i + 1 < n and s * (c[i] - o[i]) > 0 and s * (c[i + 1] - o[i + 1]) > 0 else None
    if entry == "RETEST":
        if not (atr > 0) or i < 1:
            return None
        lvl = c[i - 1] - s * 0.5 * atr
        for k in range(i, min(n, i + RETEST_BARS)):
            if (lo[k] <= lvl < c[k]) if s > 0 else (h[k] >= lvl > c[k]):
                return k + 1
        return None
    raise ValueError(entry)


def stop_distance(stop: str, F: dict, j: int, s: float, e: float):
    atr = float(F["atr14"][j])
    if not (atr > 0):
        return None
    if stop.startswith("ATR_"):
        return {"ATR_1_0": 1.0, "ATR_1_5": 1.5, "ATR_2_0": 2.0}[stop] * atr
    ext = float(F["lo8"][j]) if s > 0 else float(F["hi8"][j])
    if not math.isfinite(ext):
        return None
    if stop == "SWING_8":
        return max(s * (e - ext) + 0.1 * atr, 0.5 * atr)
    if stop == "VOL_STRUCT":
        return max(s * (e - ext) + 0.25 * atr, 1.0 * atr)
    raise ValueError(stop)


def simulate(arr, j: int, s: float, e: float, d: float) -> dict | None:
    """Fixed exit from open of bar j. Returns gross R + path metrics (32 bars)."""
    o, h, lo, c = arr
    if j + MAX_HOLD_BARS > len(o):
        return None
    stop = e - s * d
    gross, k_exit, reason = None, None, None
    mfe = mae = 0.0
    t_mfe = t_mae = 0
    first = {"stop": None, "0.5": None, "1": None, "1.5": None, "2": None}
    iae = None
    for n, k in enumerate(range(j, j + MAX_HOLD_BARS), start=1):
        fav = s * ((h[k] if s > 0 else lo[k]) - e) / d
        adv = s * ((lo[k] if s > 0 else h[k]) - e) / d
        if iae is None:
            iae = adv
        if adv < mae:
            mae, t_mae = adv, n
        if fav > mfe:
            mfe, t_mfe = fav, n
        if first["stop"] is None and adv <= -1.0:
            first["stop"] = n
        for key in ("0.5", "1", "1.5", "2"):
            if first[key] is None and fav >= float(key) and (first["stop"] is None or first["stop"] > n):
                first[key] = n                           # adverse first within a bar
        if gross is None:
            if adv <= -1.0:
                fill = min(stop, o[k]) if s > 0 else max(stop, o[k])
                gross, k_exit, reason = s * (fill - e) / d, k, "STOP"
            elif fav >= TARGET_R:
                gross, k_exit, reason = TARGET_R, k, "TARGET"
            elif n == MAX_HOLD_BARS:
                gross, k_exit, reason = s * (c[k] - e) / d, k, "TIME"
    before = {f"plus{key.replace('.', '_')}r_before_stop": first[key] is not None for key in ("0.5", "1", "1.5", "2")}
    return {"gross_r": float(gross), "k_exit": k_exit, "exit_reason": reason,
            "path": {"iae_r": float(iae), "mfe_r": float(mfe), "mae_r": float(mae), "t_mfe": t_mfe, "t_mae": t_mae,
                     "stop_first": first["stop"] is not None and first["1"] is None,
                     "stop_touched": first["stop"] is not None, **before,
                     "stopped_then_plus1r": reason == "STOP" and mfe >= 1.0 and first["1"] is None,
                     "ret_atr": {}}}


def _regime(F, j):
    t4, r4 = F["trend4h"][j], F["range4h"][j]
    return "TREND_UP" if t4 == 1 else "TREND_DOWN" if t4 == -1 else "RANGE" if r4 == 1 else "MIXED"


def cost_fracs(symbol: str) -> dict:
    slip = SLIPPAGE_MAJOR if symbol in MAJORS else SLIPPAGE_ALT
    return {"fee": 2 * TAKER_FEE, "slip": 2 * slip, "fund": FUNDING_ALLOWANCE}


def costed(gross: float, stop_frac: float, symbol: str) -> dict:
    cf = cost_fracs(symbol)
    fee, slip, fund = cf["fee"] / stop_frac, cf["slip"] / stop_frac, cf["fund"] / stop_frac
    b = UNCERTAINTY_BUFFER_R
    return {"fee_r": fee, "slip_r": slip, "fund_r": fund, "buffer_r": b, "r": gross - fee - slip - fund - b,
            "cost_r": {"current": gross - fee - slip - fund - b, "fees_plus_50pct": gross - 1.5 * fee - slip - fund - b,
                       "slippage_x2": gross - fee - 2 * slip - fund - b,
                       "combined_adverse": gross - 1.5 * fee - 2 * slip - 2 * fund - b}}


def unit_id(setup, entry, stop, side) -> str:
    return f"{setup}|{entry}|{stop}|{side}"


def units():
    for su in SETUPS:
        for en in ENTRIES:
            for st in STOPS:
                for sd in SIDES:
                    yield su, en, st, sd


def generate_symbol(symbol: str, c15, c1h, c4h, *, span=(DECISION_START_MS, DECISION_END_MS),
                    setups=SETUPS, entries=ENTRIES, stops=STOPS, event_override=None) -> list[dict]:
    F = af.series(c15, c1h, c4h)
    arr = tuple(np.array([float(b[k]) for b in c15]) for k in ("o", "h", "l", "c"))
    ts = F["ts"]
    n = len(ts)
    trades = []
    for su in setups:
        for side in SIDES:
            s = 1.0 if side == "LONG" else -1.0
            ev = event_override(su, side) if event_override else events(F, su, s)
            idx = [i for i in np.flatnonzero(ev) if span[0] <= ts[i] < span[1] and i > af.W15]
            for en in entries:
                for st in stops:
                    free = -1
                    for i in idx:
                        if i <= free:
                            continue
                        j = entry_index(arr, int(i), en, s, float(F["atr14"][i]))
                        if j is None or j >= n or j <= free:
                            continue
                        e = float(arr[0][j])
                        d = stop_distance(st, F, j, s, e)
                        if d is None or not (MIN_STOP_FRAC <= d / e <= MAX_STOP_FRAC):
                            continue
                        sim = simulate(arr, j, s, e, d)
                        if sim is None:
                            continue
                        free = sim["k_exit"]
                        sf = d / e
                        cst = costed(sim["gross_r"], sf, symbol)
                        t = int(ts[j])
                        for hb in (4, 16, 32):
                            sim["path"]["ret_atr"][str(hb)] = s * (arr[3][j + hb - 1] - e) / float(F["atr14"][j])
                        trades.append({
                            "ts": t, "event_ts": int(ts[i]), "outcome_end_ts": int(ts[sim["k_exit"]]) + BAR_MS,
                            "outcome_status": "RESOLVED", "symbol": symbol, "direction": side, "setup": su,
                            "entry": en, "stop": st, "unit": unit_id(su, en, st, side),
                            "month": dt.datetime.fromtimestamp(t / 1000, dt.timezone.utc).strftime("%Y-%m"),
                            "ai_regime": _regime(F, j), "gross_r": sim["gross_r"], "exit_reason": sim["exit_reason"],
                            "stop_frac": sf, "notional_per_risk": 1.0 / sf, **cst, "path": sim["path"],
                            "x": af.model_vector(F, j, s, sf, sum(cost_fracs(symbol).values()))})
    return trades


def generate(data: dict, **kw) -> list[dict]:
    out = []
    for s in sorted(data):
        out += generate_symbol(s, data[s]["15"], data[s]["60"], data[s]["240"], **kw)
    out.sort(key=lambda r: (r["ts"], r["symbol"], r["unit"]))
    return out


def trades_sha256(trades) -> str:
    canon = [[t["ts"], t["symbol"], t["unit"], round(t["gross_r"], 10), round(t["r"], 10)] for t in trades]
    return hashlib.sha256(json.dumps(canon, separators=(",", ":")).encode()).hexdigest()


# ── statistics helpers ─────────────────────────────────────────────────────
def stats(xs) -> dict:
    x = [float(v) for v in xs if v is not None]
    n = len(x)
    if not n:
        return {"n": 0}
    wins, losses = [v for v in x if v > 0], [-v for v in x if v < 0]
    return {"n": n, "mean_r": sum(x) / n, "median_r": float(np.median(x)), "win_rate": len(wins) / n,
            "profit_factor": (sum(wins) / sum(losses)) if losses else None,
            "avg_win_r": (sum(wins) / len(wins)) if wins else None,
            "avg_loss_r": (-sum(losses) / len(losses)) if losses else None, "total_r": sum(x)}


def max_drawdown(rows) -> float:
    eq = peak = dd = 0.0
    for r in sorted(rows, key=lambda r: (r["outcome_end_ts"], r["ts"])):
        eq += float(r["r"])
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    return dd


def concentration(rows, key) -> dict:
    tot = defaultdict(float)
    for r in rows:
        tot[str(r.get(key))] += float(r["r"])
    pos = {k: v for k, v in tot.items() if v > 0}
    ps = sum(pos.values())
    top = max(pos.items(), key=lambda kv: kv[1]) if pos else (None, 0.0)
    return {"groups": len(tot), "groups_positive": len(pos), "top_group": top[0],
            "top_share_of_positive_r": (top[1] / ps) if ps > 0 else None}


def _mean(rows, key="r"):
    return float(np.mean([float(r[key]) if key == "r" else float(r["cost_r"][key]) for r in rows])) if rows else None


# ── pre-gate ───────────────────────────────────────────────────────────────
def pre_gate(rows) -> dict:
    fails = []
    n = len(rows)
    if n < PRE_MIN_TRADES:
        fails.append("TOO_FEW_TRADES")
    m = _mean(rows)
    if not (m is not None and m > 0):
        fails.append("NET_MEAN_NOT_POSITIVE")
    ca = _mean(rows, "combined_adverse")
    if not (ca is not None and ca > 0):
        fails.append("COMBINED_ADVERSE_NOT_POSITIVE")
    bym = defaultdict(list)
    for r in rows:
        bym[r["month"]].append(float(r["r"]))
    months = {k: v for k, v in bym.items() if len(v) >= PRE_MIN_MONTH_TRADES}
    if len(months) < PRE_MIN_MONTHS or sum(np.mean(v) > 0 for v in months.values()) * 2 < len(months):
        fails.append("NOT_POSITIVE_ACROSS_MONTHS")
    if len({r["symbol"] for r in rows}) < PRE_MIN_SYMBOLS:
        fails.append("TOO_FEW_SYMBOLS")
    for k, lim, code in (("symbol", MAX_TOP_SYMBOL_SHARE, "ONE_SYMBOL"),
                         ("ai_regime", MAX_TOP_REGIME_SHARE, "ONE_REGIME")):
        sh = concentration(rows, k)["top_share_of_positive_r"]
        if rows and (sh is None or sh > lim):
            fails.append(code)
    return {"pass": not fails, "failures": fails, "n": n, "mean_r": m, "combined_adverse_mean_r": ca}


# ── folds / models / search ────────────────────────────────────────────────
def make_folds(trades):
    if not trades:
        return None, {"status": "NO_TRADES"}
    t0 = min(DECISION_START_MS, min(t["ts"] for t in trades))
    t1 = max(t["ts"] for t in trades) + 1
    lay = inf.purged_calendar_folds(t0, t1, required_horizon_ms=max(REQUIRED_MS, MAX_HOLD_BARS * BAR_MS))
    if lay["status"] != "OK":
        return None, lay
    W = lay["windows"]
    return [[t for t in trades if w["decision_start_ts"] <= t["ts"] < w["decision_end_ts"]
             and t["outcome_end_ts"] < w["outcome_window_end_ts"]] for w in W], lay


def _X(rows):
    return np.array([r["x"] for r in rows], float).reshape(len(rows), len(af.MODEL_FEATURES))


class FittedModel:
    """A model + its calibration + threshold; deterministic JSON round trip."""

    def __init__(self, kind, hp, threshold, model, calibrator=None):
        self.kind, self.hp, self.threshold, self.model, self.calibrator = kind, dict(hp), float(threshold), model, calibrator

    def score(self, X):
        X = np.asarray(X, float).reshape(-1, len(af.MODEL_FEATURES))
        if self.kind == "RIDGE_NET":
            return np.asarray(self.model.predict(X), float)
        p = np.asarray(self.model.predict_proba(X), float)
        return np.asarray(self.calibrator.transform(p), float) if self.calibrator is not None else p

    def approve(self, rows):
        if not rows:
            return []
        sc = self.score(_X(rows))
        return [r for r, v in zip(rows, sc) if (v > self.threshold if self.kind == "RIDGE_NET" else v >= self.threshold)]

    def to_json(self):
        return {"kind": self.kind, "hp": self.hp, "threshold": self.threshold, "model": self.model.params(),
                "calibration": self.calibrator.to_json() if self.calibrator is not None else None,
                "feature_schema": af.VERSION, "feature_schema_sha256": af.SCHEMA_SHA256,
                "features": list(af.MODEL_FEATURES)}

    @classmethod
    def from_json(cls, j):
        model = (mdl.Ridge if j["kind"] == "RIDGE_NET" else mdl.LogisticL2).from_params(j["model"])
        c = cal.from_json(j["calibration"]) if j.get("calibration") else None
        return cls(j["kind"], j["hp"], j["threshold"], model, c)


def _fit_models(train, val):
    X, Xv = _X(train), _X(val)
    y = np.array([float(r["r"]) for r in train])
    out = []
    for kind, hp, grid in MODELS:
        if kind == "RIDGE_NET":
            m = mdl.Ridge(**hp).fit(X, y)
            out += [(FittedModel(kind, hp, th, m), None) for th in grid]
        else:
            yb = (y > 0).astype(float)
            if yb.min() == yb.max():
                continue
            m = mdl.LogisticL2(**hp).fit(X, yb)
            yv = np.array([float(r["r"] > 0) for r in val])
            c = cal.Platt().fit(m.predict_proba(Xv), yv) if len(val) and 0 < yv.mean() < 1 else None
            rep = cal.report(c.transform(m.predict_proba(Xv)) if c else m.predict_proba(Xv), yv) if len(val) else None
            out += [(FittedModel(kind, hp, th, m, c), rep) for th in grid]
    return out


STEPS = (([0], 1, 2), ([0, 1], 2, 3))


def search(trades, folds) -> dict:
    by_fold_unit = [defaultdict(list) for _ in folds]
    for k, f in enumerate(folds):
        for t in f:
            by_fold_unit[k][t["unit"]].append(t)
    ledger, steps = [], []
    for si, (tr_idx, va, ev) in enumerate(STEPS):
        best = None
        for su, en, st, sd in units():
            u = unit_id(su, en, st, sd)
            train = [t for k in tr_idx for t in by_fold_unit[k].get(u, [])]
            val, test = by_fold_unit[va].get(u, []), by_fold_unit[ev].get(u, [])
            pg = pre_gate(train)
            base = {"step": si + 1, "unit": u, "setup": su, "entry": en, "stop": st, "side": sd,
                    "search_spec_sha256": SPEC_SHA256, "pre_gate": pg,
                    "raw_validation": stats([t["r"] for t in val]), "raw_outer_posthoc": stats([t["r"] for t in test])}
            ledger.append({**base, "candidate_id": f"{u}|RAW", "model": "RAW", "promotable": False})
            if not pg["pass"]:
                continue
            for fm, rep in _fit_models(train, val):
                cid = f"{u}|{fm.kind}|{fm.threshold}"
                reason = "OK"
                if fm.kind == "LOGISTIC_PROB" and not (rep and rep.get("beats_base_rate")):
                    reason = "CALIBRATION_DOES_NOT_BEAT_BASE_RATE"
                av = fm.approve(val) if reason == "OK" else []
                ao = fm.approve(test) if reason == "OK" else []
                vm = _mean(av)
                ok = reason == "OK" and len(av) >= MIN_VALIDATION_TRADES and vm is not None and vm > 0
                if reason == "OK" and not ok:
                    reason = "VALIDATION_TOO_FEW_OR_NOT_POSITIVE"
                ledger.append({**base, "candidate_id": cid, "model": fm.kind, "threshold": fm.threshold,
                               "promotable": True, "reason": reason,
                               "validation": {"n": len(av), "mean_r": vm,
                                              "calibration": {k: (rep or {}).get(k) for k in
                                                              ("brier", "brier_base_rate", "ece", "beats_base_rate")}},
                               "outer_posthoc_not_used_for_selection": stats([t["r"] for t in ao]),
                               "selected": False})
                if ok and (best is None or vm > best[0]):
                    best = (vm, len(ledger) - 1, fm, u, su, sd, len(av), test, ao)
        if best is None:
            steps.append({"step": si + 1, "selected": None, "reason": "NO_CANDIDATE_PASSES_PREGATE_AND_VALIDATION",
                          "test": [], "approved": []})
            continue
        vm, li, fm, u, su, sd, nval, test, ao = best
        ledger[li]["selected"] = True
        yt = np.array([float(t["r"] > 0) for t in test])
        steps.append({"step": si + 1, "selected": {"candidate_id": ledger[li]["candidate_id"], "unit": u, "setup": su,
                                                   "side": sd, "entry": ledger[li]["entry"], "stop": ledger[li]["stop"],
                                                   "model": fm.to_json(), "validation_mean_r": vm,
                                                   "validation_trades": nval},
                      "fitted": fm, "test": test, "approved": ao,
                      "probability_authorizes": fm.kind == "LOGISTIC_PROB",
                      "test_calibration": cal.report(fm.score(_X(test)), yt) if (test and fm.kind == "LOGISTIC_PROB")
                      else None})
    return {"ledger": ledger, "steps": steps}


def gate(steps, *, parity_ok: bool) -> dict:
    sel = [s["selected"] for s in steps]
    fails = []
    if not parity_ok:
        fails.append("RUNTIME_FEATURE_PARITY_FAILED")
    if len(sel) != 2 or not all(sel):
        fails.append("ABSTAIN_IN_SOME_STEP")
    stable = {"direction": bool(all(sel) and len({s["side"] for s in sel}) == 1),
              "setup": bool(all(sel) and len({s["setup"] for s in sel}) == 1)}
    if not stable["direction"]:
        fails.append("DIRECTION_UNSTABLE")
    if not stable["setup"]:
        fails.append("SETUP_UNSTABLE")
    test = sorted([t for s in steps for t in s["test"]], key=lambda r: r["ts"])
    ids = {id(t) for s in steps for t in s["approved"]}
    appr = [t for t in test if id(t) in ids]
    rep = {"stability": stable, "architecture_baseline": {"n": len(test), "mean_r": _mean(test)},
           "pooled_approved": stats([t["r"] for t in appr])}
    if len(appr) < MIN_POOLED_TRADES:
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
    if not (exp.get("authority_status") == inf.AUTHORITY_VALID and (exp.get("authority_ci_low") or -1) > 0):
        fails.append("EXPECTANCY_CI_NOT_POSITIVE")
    if not (up.get("delta") or 0) > 0:
        fails.append("UPLIFT_NOT_POSITIVE")
    if not (up.get("authority_status") == inf.AUTHORITY_VALID and (up.get("authority_ci_low") or -1) > 0):
        fails.append("UPLIFT_CI_NOT_POSITIVE")
    rep["cost_stress"] = {k: _mean(appr, k) for k in ("current",) + REQUIRED_COST_SCENARIOS}
    for sc in REQUIRED_COST_SCENARIOS:
        if not (rep["cost_stress"][sc] or -1) > 0:
            fails.append(f"COST_STRESS_FAILS_{sc.upper()}")
    rep["concentration"] = {k: concentration(appr, k) for k in ("symbol", "month", "ai_regime", "direction")}
    for k, lim, code in (("symbol", MAX_TOP_SYMBOL_SHARE, "SINGLE_SYMBOL_DOMINATES"),
                         ("month", MAX_TOP_MONTH_SHARE, "SINGLE_MONTH_DOMINATES"),
                         ("ai_regime", MAX_TOP_REGIME_SHARE, "SINGLE_REGIME_DOMINATES")):
        sh = rep["concentration"][k]["top_share_of_positive_r"]
        if sh is None or sh > lim:
            fails.append(code)
    rep["max_drawdown_r"] = max_drawdown(appr)
    rep["calibration"] = [s.get("test_calibration") for s in steps]
    if any(s.get("probability_authorizes") for s in steps):
        for s in steps:
            c = s.get("test_calibration")
            if s.get("probability_authorizes") and (c is None or not c.get("beats_base_rate")
                                                     or (c.get("ece") or 1) > MAX_ECE):
                fails.append("CALIBRATION_FAILURE_WITH_PROBABILITY_AUTHORITY")
    return {**rep, "failures": sorted(set(fails)), "all_pass": not fails, "label": EVIDENCE_LABEL}


# ── analyses (diagnostic; full sample; PREVIOUSLY_INSPECTED) ───────────────
def _group(trades, keyfn):
    g = defaultdict(list)
    for t in trades:
        g[keyfn(t)].append(t)
    return g


def architecture_baselines(trades, folds) -> dict:
    fold_of = {id(t): k + 1 for k, f in enumerate(folds or []) for t in f}
    out = {"role": "DIAGNOSTIC_RAW_BEFORE_ML", "label": EVIDENCE_LABEL, "units": {}}
    for u, rows in sorted(_group(trades, lambda t: t["unit"]).items()):
        d = {**stats([t["r"] for t in rows]), "gross": stats([t["gross_r"] for t in rows]),
             "cost_stress": {k: _mean(rows, k) for k in REQUIRED_COST_SCENARIOS},
             "mean_cost_r": float(np.mean([t["fee_r"] + t["slip_r"] + t["fund_r"] + t["buffer_r"] for t in rows])),
             "by_fold": {str(k): stats([t["r"] for t in rows if fold_of.get(id(t)) == k]) for k in (1, 2, 3, 4)}}
        out["units"][u] = {k: v for k, v in d.items()}
    agg = _group(trades, lambda t: f"{t['setup']}|{t['direction']}")
    out["by_setup_side"] = {k: {**stats([t["r"] for t in v]), "gross_mean_r": float(np.mean([t["gross_r"] for t in v]))}
                            for k, v in sorted(agg.items())}
    return out


def direction_analysis(trades, folds) -> dict:
    fold_of = {id(t): k + 1 for k, f in enumerate(folds or []) for t in f}
    ref = [t for t in trades if t["entry"] == "IMMEDIATE" and t["stop"] == "ATR_1_5"]
    out = {"role": "DIAGNOSTIC", "reference": "IMMEDIATE entry (stop-independent path from the entry open)",
           "setups": {}}
    for k, rows in sorted(_group(ref, lambda t: f"{t['setup']}|{t['direction']}").items()):
        d = {"events": len(rows)}
        for hb in ("4", "16", "32"):
            v = [t["path"]["ret_atr"][hb] for t in rows]
            d[f"hit_rate_{int(hb) * 15}m"] = float(np.mean([x > 0 for x in v])) if v else None
            d[f"mean_move_atr_{int(hb) * 15}m"] = float(np.mean(v)) if v else None
        d["by_fold_hit_rate_480m"] = {str(f): (float(np.mean([t["path"]["ret_atr"]["32"] > 0 for t in rows
                                                              if fold_of.get(id(t)) == f]))
                                               if any(fold_of.get(id(t)) == f for t in rows) else None)
                                      for f in (1, 2, 3, 4)}
        out["setups"][k] = d
    return out


def entry_timing_analysis(trades) -> dict:
    out = {"role": "DIAGNOSTIC", "by_setup_side_stop": {}}
    g = _group(trades, lambda t: (t["setup"], t["direction"], t["stop"]))
    for (su, sd, st), rows in sorted(g.items()):
        by_e = _group(rows, lambda t: t["entry"])
        imm = {(t["symbol"], t["event_ts"]): t for t in by_e.get("IMMEDIATE", [])}
        d = {}
        for en in ENTRIES:
            er = by_e.get(en, [])
            paired = [(t["r"] - imm[(t["symbol"], t["event_ts"])]["r"]) for t in er
                      if (t["symbol"], t["event_ts"]) in imm]
            d[en] = {**stats([t["r"] for t in er]), "gross_mean_r": float(np.mean([t["gross_r"] for t in er])) if er else None,
                     "paired_vs_immediate": {"n": len(paired), "mean_delta_r": float(np.mean(paired)) if paired else None}}
        out["by_setup_side_stop"][f"{su}|{sd}|{st}"] = d
    agg = _group(trades, lambda t: t["entry"])
    out["by_entry"] = {k: stats([t["r"] for t in v]) for k, v in sorted(agg.items())}
    return out


def stop_geometry_analysis(trades) -> dict:
    out = {"role": "DIAGNOSTIC", "capital_at_risk": "fixed 1R per trade; notional_per_risk = 1/stop_frac",
           "by_stop": {}, "by_setup_side_entry": {}}
    for st, rows in sorted(_group(trades, lambda t: t["stop"]).items()):
        out["by_stop"][st] = {**stats([t["r"] for t in rows]), "gross_mean_r": float(np.mean([t["gross_r"] for t in rows])),
                              "mean_stop_frac": float(np.mean([t["stop_frac"] for t in rows])),
                              "mean_notional_per_risk": float(np.mean([t["notional_per_risk"] for t in rows])),
                              "mean_cost_r": float(np.mean([t["fee_r"] + t["slip_r"] + t["fund_r"] for t in rows])),
                              "min_gross_r": float(min(t["gross_r"] for t in rows)),
                              "stop_exit_rate": float(np.mean([t["exit_reason"] == "STOP" for t in rows])),
                              "plus1r_before_stop": float(np.mean([t["path"]["plus1r_before_stop"] for t in rows])),
                              "stopped_then_plus1r": float(np.mean([t["path"]["stopped_then_plus1r"] for t in rows]))}
    for k, rows in sorted(_group(trades, lambda t: (t["setup"], t["direction"], t["entry"])).items()):
        out["by_setup_side_entry"]["|".join(k)] = {st: stats([t["r"] for t in v])
                                                   for st, v in sorted(_group(rows, lambda t: t["stop"]).items())}
    return out


def mfe_mae_analysis(trades) -> dict:
    out = {"role": "DIAGNOSTIC", "units": {}}
    keys = ("iae_r", "mfe_r", "mae_r", "t_mfe", "t_mae")
    flags = ("stop_first", "plus0_5r_before_stop", "plus1r_before_stop", "plus1_5r_before_stop", "plus2r_before_stop",
             "stopped_then_plus1r")
    for u, rows in sorted(_group(trades, lambda t: t["unit"]).items()):
        out["units"][u] = {**{f"mean_{k}": float(np.mean([t["path"][k] for t in rows])) for k in keys},
                           **{k: float(np.mean([t["path"][k] for t in rows])) for k in flags}, "n": len(rows)}
    return out


def nexus_stop_analysis(rows) -> dict:
    """The Phase-8D NEXUS hook population: classify stopped losers that later reached +1R."""
    from bot.ai import training as tr
    data = tr.dataset(rows)
    def path(r, flip=False):
        fwd, e, sl = r.get("_fwd") or [], r.get("entry_fill"), r.get("stop")
        if len(fwd) < 2 or not e or sl is None or e == sl:
            return None
        s = (1.0 if str(r["direction"]).upper() == "LONG" else -1.0) * (-1.0 if flip else 1.0)
        d = abs(float(e) - float(sl))
        ks = k1 = None
        mae_before = 0.0
        for k, (_, o, h, lo, c) in enumerate(fwd[:16], start=1):
            adv = s * ((lo if s > 0 else h) - float(e)) / d
            fav = s * ((h if s > 0 else lo) - float(e)) / d
            if ks is None and adv <= -1.0:
                ks = k
            if k1 is None and fav >= 1.0 and not (ks == k):
                k1 = k
            if k1 is None:
                mae_before = min(mae_before, adv)
        return {"ks": ks, "k1": k1, "mae_before_plus1": mae_before}
    losers = [r for r in data if float(r["r"]) < 0]
    cls = defaultdict(int)
    reasons = defaultdict(int)
    for r in losers:
        reasons[str(r.get("exit_reason"))] += 1
        p = path(r)
        if p is None:
            cls["NO_PATH"] += 1
        elif p["k1"] is None:
            cls["NEVER_REACHED_PLUS1R"] += 1
        elif p["ks"] is None or p["k1"] < p["ks"]:
            cls["REACHED_PLUS1R_BEFORE_STOP_THEN_CLOSED_LE_0 (exit giveback)"] += 1
        elif p["mae_before_plus1"] <= -1.5:
            cls["STOP_FIRST_DEEP_ADVERSE_THEN_PLUS1R (directionally correct but early)"] += 1
        else:
            cls["STOP_FIRST_SHALLOW_THEN_PLUS1R (structure right, stop over-tight)"] += 1
    def rate(flip):
        ps = [path(r, flip) for r in data]
        ps = [p for p in ps if p is not None]
        hit = [p["ks"] is not None and p["k1"] is not None and p["k1"] > p["ks"] for p in ps]
        return (float(np.mean(hit)) if hit else None), len(hit)
    ro, no = rate(False)
    rf, nf = rate(True)
    se = math.sqrt(ro * (1 - ro) / no + rf * (1 - rf) / nf) if no and nf and ro is not None and rf is not None else None
    z = (ro - rf) / se if se else None
    verdict = ("RANDOM_RECOVERY_CONSISTENT" if z is not None and abs(z) < 2
               else "NEXUS_DIRECTION_RECOVERS_MORE_THAN_FLIPPED" if z is not None and z >= 2
               else "NEXUS_DIRECTION_RECOVERS_LESS_THAN_FLIPPED" if z is not None else "UNAVAILABLE")
    return {"rows": len(data), "losers": len(losers), "loser_exit_reasons": dict(reasons),
            "loser_classes_within_4h": dict(cls),
            "stop_then_plus1r_rate": {"nexus_direction": ro, "flipped_direction": rf, "n": no, "z": z},
            "random_recovery_verdict": verdict,
            "note": "path = 16 closed 15m bars from the decision bar (Phase 8D _fwd); stop = replay final stop"}


# ── orchestration ──────────────────────────────────────────────────────────
def run_search(trades, *, parity_ok: bool) -> dict:
    folds, lay = make_folds(trades)
    if folds is None:
        return {"status": "INSUFFICIENT_INDEPENDENT_FOLDS", "result": "NO_VALID_CHALLENGER", "layout": lay,
                "ledger": [], "steps": [], "gate": {"all_pass": False, "failures": ["NO_FOLDS"]}}
    s = search(trades, folds)
    g = gate(s["steps"], parity_ok=parity_ok)
    promo = [e for e in s["ledger"] if e.get("promotable")
             and (e.get("outer_posthoc_not_used_for_selection") or {}).get("n", 0) >= MIN_VALIDATION_TRADES]
    raw = [e for e in s["ledger"] if not e.get("promotable") and e["raw_outer_posthoc"].get("n", 0) >= MIN_VALIDATION_TRADES]
    bp = max(promo, key=lambda e: e["outer_posthoc_not_used_for_selection"]["mean_r"], default=None)
    br = max(raw, key=lambda e: e["raw_outer_posthoc"]["mean_r"], default=None)
    days = sum((f[-1]["ts"] - f[0]["ts"]) / 86_400_000.0 for f in folds[2:] if f)
    return {"status": "OK", "folds": [len(f) for f in folds], "layout": {k: lay.get(k) for k in ("status",)},
            "ledger": s["ledger"], "steps": s["steps"], "gate": g,
            "result": "CANDIDATE_SUPPORTED" if g["all_pass"] else "NO_VALID_CHALLENGER",
            "best_posthoc_outer": {
                "model_candidate": None if bp is None else {"candidate_id": bp["candidate_id"], "step": bp["step"],
                                                            **bp["outer_posthoc_not_used_for_selection"]},
                "raw_architecture": None if br is None else {"candidate_id": br["candidate_id"], "step": br["step"],
                                                             **br["raw_outer_posthoc"]},
                "note": "data-snooping diagnostic only; never used for selection or freeze; cannot be promoted"},
            "estimate": {"outer_eval_days": days, "approved": g["pooled_approved"].get("n", 0),
                         "estimated_trades_per_72h": (g["pooled_approved"].get("n", 0) / days * 3) if days else None},
            "_folds": folds}


def public_steps(steps):
    return [{k: v for k, v in st.items() if k not in ("test", "approved", "fitted")}
            | {"outer_eval": {"architecture_baseline": stats([t["r"] for t in st["test"]]),
                              "approved": stats([t["r"] for t in st["approved"]])}} for st in steps]


# ── freeze / export ────────────────────────────────────────────────────────
PROBE = [[round(math.sin(i * 7 + k) * 2, 6) for k in range(len(af.MODEL_FEATURES))] for i in range(16)]


def _canon(o) -> bytes:
    return json.dumps(o, sort_keys=True, separators=(",", ":")).encode()


def freeze(result, *, dataset_sha: str, trades_sha: str, code_sha: str, created_at: str) -> dict:
    if result.get("result") != "CANDIDATE_SUPPORTED":
        raise ValueError("no supported candidate; refusing to freeze")
    st = result["steps"][-1]
    sel, fm = st["selected"], st["fitted"]
    arch = {"setup": sel["setup"], "entry": sel["entry"], "stop": sel["stop"], "side": sel["side"],
            "setup_rule": SPEC["setups"][sel["setup"]], "entry_rule": SPEC["entries"][sel["entry"]],
            "stop_rule": SPEC["stops"][sel["stop"]], "exit_policy": EXIT_POLICY}
    policy = {"candidate_id": sel["candidate_id"], "model_kind": fm.kind, "threshold": fm.threshold,
              "probability_authorizes": fm.kind == "LOGISTIC_PROB", "order_authority": False, "live_authority": False}
    model = fm.to_json()
    probe = [float(v) for v in fm.score(np.array(PROBE))]
    manifest = {"schema": "SIGNAL_ARCHITECTURE_BUNDLE_V1", "lifecycle_state": "SHADOW_CHALLENGER",
                "evidence_label": EVIDENCE_LABEL, "search_spec_sha256": SPEC_SHA256,
                "cost_policy_sha256": COST_POLICY_SHA256, "exit_policy_sha256": EXIT_POLICY_SHA256,
                "feature_schema": af.VERSION, "feature_schema_sha256": af.SCHEMA_SHA256,
                "training_dataset_sha256": dataset_sha, "training_trades_sha256": trades_sha,
                "training_code_sha": code_sha, "created_at": created_at,
                "architecture_sha256": hashlib.sha256(_canon(arch)).hexdigest(),
                "policy_sha256": hashlib.sha256(_canon(policy)).hexdigest(),
                "model_sha256": hashlib.sha256(_canon(model)).hexdigest(), "probe_scores": probe}
    manifest["bundle_sha256"] = hashlib.sha256(_canon(manifest)).hexdigest()
    return {"architecture": arch, "policy": policy, "model": model, "manifest": manifest}


def export(bundle: dict, out_dir) -> dict:
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
    if b["model"]["feature_schema_sha256"] != af.SCHEMA_SHA256:
        fails.append("FEATURE_SCHEMA")
    fm = FittedModel.from_json(b["model"])
    s1, s2 = fm.score(np.array(PROBE)), fm.score(np.array(PROBE))
    det = bool(np.array_equal(s1, s2)) and np.allclose(s1, m["probe_scores"], rtol=0, atol=1e-12)
    if not det:
        fails.append("NON_DETERMINISTIC_INFERENCE")
    return {"verified": not fails, "failures": fails, "bundle_sha256": bsha, "lifecycle_state": m["lifecycle_state"],
            "deterministic_inference": det}


# ── CLI ────────────────────────────────────────────────────────────────────
def main(argv=None) -> int:
    import argparse
    import asyncio
    from pathlib import Path
    ap = argparse.ArgumentParser(description="Phase 8E signal architecture research (research only)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("--out", required=True)
    r = sub.add_parser("run")
    r.add_argument("--dataset", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--candidate-sha", required=True)
    r.add_argument("--created-at", required=True)
    r.add_argument("--nexus-rows", default=None)
    a = ap.parse_args(argv)
    if a.cmd == "fetch":
        data = asyncio.run(fetch_dataset())
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        sha = save_dataset(data, a.out)
        print(json.dumps({"dataset_sha256": sha, "bars": {s: {tf: len(v) for tf, v in d.items()}
                                                          for s, d in data.items()}}, sort_keys=True))
        return 0
    data, header = load_dataset(a.dataset)
    check_no_forward_evidence(data)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    def dump(name, obj):
        (out / name).write_text(json.dumps(obj, indent=1, sort_keys=True, default=str) + "\n")
    dump("signal_architecture_spec.json", {"spec": SPEC, "spec_sha256": SPEC_SHA256,
                                           "cost_policy_sha256": COST_POLICY_SHA256,
                                           "exit_policy_sha256": EXIT_POLICY_SHA256,
                                           "feature_schema": af.SCHEMA, "feature_schema_sha256": af.SCHEMA_SHA256})
    parity = {s: af.parity_report(data[s]["15"], data[s]["60"], data[s]["240"]) for s in sorted(data)}
    parity_ok = all(p["parity"] for p in parity.values())
    dump("feature_parity_report.json", {"parity": parity_ok, "per_symbol": parity,
                                        "research_only_8d_features_promoted": False})
    trades = generate(data)
    tsha = trades_sha256(trades)
    res = run_search(trades, parity_ok=parity_ok)
    folds = res.get("_folds")
    dump("architecture_baselines.json", architecture_baselines(trades, folds))
    dump("direction_analysis.json", direction_analysis(trades, folds))
    dump("entry_timing_analysis.json", entry_timing_analysis(trades))
    dump("stop_geometry_analysis.json", stop_geometry_analysis(trades))
    mm = mfe_mae_analysis(trades)
    if a.nexus_rows and Path(a.nexus_rows).exists():
        with gzip.open(a.nexus_rows, "rt", encoding="utf-8") as fh:
            fh.readline()
            nrows = [json.loads(line) for line in fh if line.strip()]
        from bot.ai import training as tr
        mm["nexus_phase8d"] = {"dataset_sha256": tr.dataset_manifest(nrows)["sha256"], **nexus_stop_analysis(nrows)}
    else:
        mm["nexus_phase8d"] = {"status": "UNAVAILABLE"}
    dump("mfe_mae_analysis.json", mm)
    with gzip.open(out / "architecture_ledger.json.gz", "wt", encoding="utf-8") as fh:
        fh.write(json.dumps(res["ledger"], sort_keys=True, default=str))
    summary = {"version": VERSION, "evidence_label": EVIDENCE_LABEL, "spec_sha256": SPEC_SHA256,
               "cost_policy_sha256": COST_POLICY_SHA256, "exit_policy_sha256": EXIT_POLICY_SHA256,
               "feature_schema_sha256": af.SCHEMA_SHA256, "dataset_sha256": header["sha256"],
               "trades_sha256": tsha, "trades": len(trades), "units": len(list(units())),
               "ledger_entries": len(res["ledger"]), "candidate_code_sha": a.candidate_sha,
               "parity": parity_ok, "status": res["status"], "result": res["result"], "folds": res.get("folds"),
               "steps": public_steps(res["steps"]), "gate": res["gate"], "estimate": res.get("estimate"),
               "best_posthoc_outer": res.get("best_posthoc_outer"),
               "pregate_pass_counts": {str(k): sum(1 for e in res["ledger"] if e["step"] == k
                                                   and e["model"] == "RAW" and e["pre_gate"]["pass"]) for k in (1, 2)}}
    if res["result"] == "CANDIDATE_SUPPORTED":
        b = freeze(res, dataset_sha=header["sha256"], trades_sha=tsha, code_sha=a.candidate_sha,
                   created_at=a.created_at)
        exp = export(b, out / "challenger_bundle")
        v1, v2 = verify(out / "challenger_bundle"), verify(out / "challenger_bundle")
        if not (v1["verified"] and v2["verified"] and v1 == v2):
            import shutil
            shutil.rmtree(out / "challenger_bundle")
            summary["result"] = "NO_VALID_CHALLENGER"
            summary["gate"]["failures"] = sorted(set(summary["gate"]["failures"] + ["BUNDLE_NOT_DETERMINISTIC"]))
        else:
            summary["frozen"] = {"manifest": b["manifest"], "file_sha256": exp["file_sha256"], "verify": v1}
    dump("phase8e_summary.json", summary)
    print(json.dumps({k: summary.get(k) for k in ("result", "trades", "dataset_sha256", "spec_sha256", "parity",
                                                  "pregate_pass_counts", "estimate", "best_posthoc_outer")},
                     indent=1, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
