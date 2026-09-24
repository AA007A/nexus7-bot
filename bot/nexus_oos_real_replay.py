"""Historical OOS replay for NEXUS incremental-edge evidence.

Analytics only. This module never mutates exchange state, runtime thresholds,
leverage, sizing, drawdown policy, or live release authorization.

The replay uses only candles closed at the decision timestamp, freezes NEXUS
freshness checks to that historical clock, simulates forward outcomes with the
same KuCoin market proxy assumptions used by bot.backtest, and marks historical
context parity incomplete when OI/orderbook history is unavailable. Incomplete
parity can be measured but must not be promoted to AI_EDGE_PROVEN.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from bisect import bisect_right
from collections import defaultdict
from dataclasses import asdict, replace
from pathlib import Path
from typing import Iterable

import aiohttp

from bot.backtest import _closed_window_by_ts, _timestamp_index, fetch_history
from bot.config import cfg
from bot.kucoin_execution_model import (
    adverse_fill,
    configured_taker_fee,
    fee_return_fraction,
    fetch_public_funding_history,
    funding_return_fraction,
    slippage_rate_for_symbol,
)
from bot.nexus_oos_edge_gate import CandidateOutcome, build_edge_report, edge_promotion_decision


class PublicKuCoinFuturesClient:
    """Minimal read-only KuCoin Futures public client for research workflows."""

    def __init__(self, base_url: str = "https://api-futures.kucoin.com") -> None:
        self.base_url = base_url.rstrip("/")
        self.session: aiohttp.ClientSession | None = None

    async def __aenter__(self):
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self.session is not None:
            await self.session.close()

    async def _get(self, path: str, params: dict | None = None, auth: bool = False):
        if auth:
            raise RuntimeError("public replay client does not support authenticated endpoints")
        if self.session is None:
            raise RuntimeError("client session not started")
        async with self.session.get(self.base_url + path, params=params or {}) as resp:
            resp.raise_for_status()
            payload = await resp.json()
        if not isinstance(payload, dict) or str(payload.get("code")) != "200000":
            raise RuntimeError(f"KuCoin public response invalid for {path}: {payload}")
        return payload.get("data")


def _ts_ms(candle: dict) -> int:
    ts = int(candle.get("ts", 0) or 0)
    return ts * 1000 if ts < 100_000_000_000 else ts


def _funding_at(events: list[dict], ts_ms: int) -> float | None:
    latest = None
    for event in events:
        tp = int(event.get("timepoint", 0) or 0)
        if tp <= ts_ms:
            latest = float(event.get("fundingRate", 0.0) or 0.0)
        else:
            break
    return latest


def _price_at_ts(candles: list[dict], timestamps: list[int], target_ts: int) -> float:
    idx = bisect_right(timestamps, int(target_ts)) - 1
    if idx < 0 or idx >= len(candles):
        return 0.0
    return float(candles[idx].get("c", 0.0) or 0.0)


def _simulate_net_r(
    *,
    direction: str,
    signal_entry: float,
    signal_sl: float,
    signal_tp: float,
    signal_tp1: float,
    signal_tp2: float,
    decision_idx: int,
    decision_ts: int,
    klines_15: list[dict],
    funding_events: list[dict],
    fee_rate: float,
    slippage_rate: float,
    partial: bool = True,
    break_even: bool = True,
    max_bars: int = 40,
    stagnation_bars: int | None = None,
    detail: dict | None = None,
    shift_native: bool = True,
    timestamps: list[int] | None = None,
) -> float | None:
    """Mirror KUCOIN_MARKET_PROXY_V1 and return net R for one candidate.

    Defaults reproduce the historical model exactly. The optional exit-policy
    parameters exist for one-at-a-time exit research: ``partial`` (TP1 50%),
    ``break_even`` (SL to entry after TP1), ``max_bars`` (time exit) and
    ``stagnation_bars`` (exit at close if TP1 not reached by then). ``detail``,
    when given, receives gross/fees/funding components in R.

    LEGACY MODEL. ``shift_native=True`` (the historical behaviour) shifts SL/TP
    by the fill delta; production keeps the exchange-native SL/TP at the
    unshifted signal levels (``shift_native=False`` isolates that correction).
    The production-parity model is
    ``nexus_oos_execution_parity.simulate_production_exit``.
    """
    market_open = float(klines_15[decision_idx]["o"])
    entry_fill = adverse_fill(market_open, direction, is_entry=True, slippage_rate=slippage_rate)
    if entry_fill <= 0 or signal_entry <= 0:
        return None

    delta = (entry_fill - signal_entry) if shift_native else 0.0
    sl = float(signal_sl) + delta
    tp = float(signal_tp) + delta
    tp1 = float(signal_tp1) + delta
    tp2 = float(signal_tp2) + delta
    risk_fraction = (abs(entry_fill - sl) / entry_fill if shift_native
                     else abs(float(signal_entry) - float(signal_sl)) / float(signal_entry))
    if risk_fraction <= 0:
        return None

    timestamps = timestamps if timestamps is not None else _timestamp_index(klines_15)
    candle_ms = 15 * 60 * 1000
    has_partial = partial and abs(tp1 - tp2) > max(abs(entry_fill), 1.0) * 1e-12
    if not partial:
        tp = tp2
    tp1_hit = False
    tp1_ts: int | None = None
    exit_ts = decision_ts
    exit_legs: list[tuple[float, float]] = []

    for j in range(decision_idx, min(decision_idx + max_bars, len(klines_15))):
        future = klines_15[j]
        if (stagnation_bars is not None and not tp1_hit
                and j - decision_idx >= stagnation_bars):
            break
        high = float(future["h"])
        low = float(future["l"])
        bar_exit_ts = _ts_ms(future) + candle_ms

        if direction == "LONG":
            if low <= sl:
                exit_legs.append((adverse_fill(sl, direction, is_entry=False, slippage_rate=slippage_rate), 0.5 if tp1_hit else 1.0))
                exit_ts = bar_exit_ts
                break
            if has_partial and not tp1_hit and high >= tp1:
                tp1_hit = True
                tp1_ts = bar_exit_ts
                exit_legs.append((adverse_fill(tp1, direction, is_entry=False, slippage_rate=slippage_rate), 0.5))
                if break_even:
                    sl = entry_fill
            if tp1_hit and high >= tp2:
                exit_legs.append((adverse_fill(tp2, direction, is_entry=False, slippage_rate=slippage_rate), 0.5))
                exit_ts = bar_exit_ts
                break
            if not has_partial and high >= tp:
                exit_legs.append((adverse_fill(tp, direction, is_entry=False, slippage_rate=slippage_rate), 1.0))
                exit_ts = bar_exit_ts
                break
        else:
            if high >= sl:
                exit_legs.append((adverse_fill(sl, direction, is_entry=False, slippage_rate=slippage_rate), 0.5 if tp1_hit else 1.0))
                exit_ts = bar_exit_ts
                break
            if has_partial and not tp1_hit and low <= tp1:
                tp1_hit = True
                tp1_ts = bar_exit_ts
                exit_legs.append((adverse_fill(tp1, direction, is_entry=False, slippage_rate=slippage_rate), 0.5))
                if break_even:
                    sl = entry_fill
            if tp1_hit and low <= tp2:
                exit_legs.append((adverse_fill(tp2, direction, is_entry=False, slippage_rate=slippage_rate), 0.5))
                exit_ts = bar_exit_ts
                break
            if not has_partial and low <= tp:
                exit_legs.append((adverse_fill(tp, direction, is_entry=False, slippage_rate=slippage_rate), 1.0))
                exit_ts = bar_exit_ts
                break

    if sum(weight for _, weight in exit_legs) < 0.999999:
        horizon = max_bars if stagnation_bars is None else min(max_bars, stagnation_bars)
        last_idx = min(decision_idx + horizon - 1, len(klines_15) - 1)
        last = float(klines_15[last_idx]["c"])
        exit_legs.append((adverse_fill(last, direction, is_entry=False, slippage_rate=slippage_rate), 0.5 if tp1_hit else 1.0))
        exit_ts = _ts_ms(klines_15[last_idx]) + candle_ms

    side = 1.0 if direction == "LONG" else -1.0
    gross = sum(side * ((fill - entry_fill) / entry_fill) * weight for fill, weight in exit_legs)
    fees = fee_return_fraction(entry_fill, exit_legs, fee_rate)
    funding, _ = funding_return_fraction(
        funding_events,
        direction,
        decision_ts,
        exit_ts,
        entry_fill,
        price_at_ts=lambda ts: _price_at_ts(klines_15, timestamps, ts),
        partial_after_ts_ms=tp1_ts,
    )
    if detail is not None:
        detail.update(
            gross_r=gross / risk_fraction,
            fees_r=-fees / risk_fraction,
            funding_r=funding / risk_fraction,
            entry_fill=entry_fill,
            stop=float(signal_sl) + delta,
            risk_fraction=risk_fraction,
            exit_ts=int(exit_ts),
        )
    return (gross - fees + funding) / risk_fraction


def _freeze_nexus_clock(nexus_ai, decision_ts_ms: int):
    class _Clock:
        def __enter__(self):
            self._old = nexus_ai.time.time
            nexus_ai.time.time = lambda: decision_ts_ms / 1000.0 + 1.0

        def __exit__(self, exc_type, exc, tb):
            nexus_ai.time.time = self._old
    return _Clock()


# ── research variants ───────────────────────────────────────────────────────
COST_SCENARIOS = {
    "current": (1.0, 1.0),
    "fees_plus_25pct": (1.25, 1.0),
    "fees_plus_50pct": (1.5, 1.0),
    "slippage_x1_5": (1.0, 1.5),
    "slippage_x2": (1.0, 2.0),
    "combined_adverse": (1.5, 2.0),
}

# One exit-policy change at a time, relative to the replay's current model.
EXIT_VARIANTS = {
    "current": {},
    "no_partial_tp": {"partial": False},
    "partial_without_break_even": {"break_even": False},
    "stagnation_16_bars_4h": {"stagnation_bars": 16},
    "time_exit_20_bars": {"max_bars": 20},
    "time_exit_80_bars": {"max_bars": 80},
}
EXITS_NOT_MODELED = (
    "trailing_stop", "choch_exit", "regime_invalidation", "signal_invalidation",
    "min_hold_90m",
)

NEXUS_SCORE_COMPONENTS = (
    "TREND_ALIGNMENT", "MOMENTUM", "VOLUME", "MARKET_STRUCTURE", "VOLATILITY",
    "DERIVATIVES", "MICROSTRUCTURE", "MULTI_TIMEFRAME", "RISK_REWARD",
)

STRATEGY_GATES_NOT_ABLATED = {
    "gates": ["RSI", "MACD", "ADX", "volume", "EMA_alignment", "market_structure",
              "BOS", "CHoCH", "MTF", "adaptive_MTF", "pullback_confirmation",
              "strategy_regime", "strategy_RR_gate", "pretrade_score(score.py)",
              "news", "market_risk_context", "microstructure"],
    "reason": ("Inline predicates inside Analyzer.analyze_mtf / runtime wrappers "
               "(adaptive_mtf_entry, pullback_confirmation_hardening) or require "
               "live-only data (order book, OI, news, market-risk feeds). Ablation "
               "needs a zero-semantic-change refactor into injectable gate "
               "predicates first; the pre-trade score gate uses live external "
               "data and is absent from this replay (parity gap)."),
}


class _NexusVariant:
    """Temporarily alter NEXUS internals for one research decision call."""

    def __init__(self, nexus_ai, name: str):
        self.nx = nexus_ai
        self.name = name
        self._restore = []

    def _set(self, obj, attr, value):
        self._restore.append((obj, attr, getattr(obj, attr)))
        setattr(obj, attr, value)

    def __enter__(self):
        import os as _os
        nx, name = self.nx, self.name
        if name.startswith("minus_"):
            weights = dict(nx.WEIGHTS)
            weights[name[len("minus_"):]] = 0.0
            self._set(nx, "WEIGHTS", weights)
        elif name == "no_regime_compat_gate":
            self._set(nx, "regime_compatibility", lambda *a, **k: 100.0)
        elif name == "no_ev_gate":
            original = nx.expected_value

            def _ev(*a, **k):
                out = dict(original(*a, **k))
                out["valid"] = True
                return out
            self._set(nx, "expected_value", _ev)
        elif name == "no_rr_net_gate":
            self._env = _os.environ.get("NEXUS_MIN_RR_NET")
            _os.environ["NEXUS_MIN_RR_NET"] = "0"
        elif name == "missing_optional_scores_zero":
            original = nx._score_components

            def _sc(models, mtf, rr_net, direction, regime):
                out = dict(original(models, mtf, rr_net, direction, regime))
                comps = out.get("components") or {}
                out["total"] = round(sum(float(comps.get(k, 0.0)) * float(w)
                                         for k, w in nx.WEIGHTS.items()), 2)
                return out
            self._set(nx, "_score_components", _sc)
        return self

    def __exit__(self, *exc):
        import os as _os
        for obj, attr, value in reversed(self._restore):
            setattr(obj, attr, value)
        if self.name == "no_rr_net_gate":
            if self._env is None:
                _os.environ.pop("NEXUS_MIN_RR_NET", None)
            else:
                _os.environ["NEXUS_MIN_RR_NET"] = self._env


NEXUS_VARIANTS = (
    tuple(f"minus_{c}" for c in NEXUS_SCORE_COMPONENTS)
    + ("no_regime_compat_gate", "no_ev_gate", "no_rr_net_gate",
       "missing_optional_scores_zero")
)
# Historical-context ablations (inputs, not internals).
CONTEXT_VARIANTS = {
    "ctx_A_candle_only": {"ticker": False, "funding": False},
    "ctx_B_candle_funding": {"ticker": False, "funding": True},
    # C (candle + available derivatives) is identical to B: funding is the
    # only derivatives input with public history; OI history is unavailable.
    "ctx_D_full_available": {"ticker": True, "funding": True},
}
NON_BINDING_THRESHOLD = -100.0


def runtime_nexus_threshold(nexus_ai) -> float:
    """The threshold production uses: nexus_ai.MIN_SCORE (NEXUS_MIN_SCORE or
    MIN_ENTRY_SCORE). The replay formerly used getattr(cfg, 'NEXUS_MIN_SCORE',
    55) which always resolved to 55 because cfg has no such attribute."""
    return float(nexus_ai.MIN_SCORE)


def _decide(nexus_ai, symbol, w15, w1h, w4h, sig, ticker, funding, threshold):
    return nexus_ai.decide(
        symbol, w15, w1h, w4h,
        entry=float(sig.entry), sl=float(sig.sl), tp=float(sig.tp),
        ticker=ticker, funding=funding, oi=None, orderbook=None,
        min_score=threshold,
    )


def _features(w15, w1h) -> dict:
    from bot import nexus_oos_research as res
    h = [float(k["h"]) for k in w15]
    l = [float(k["l"]) for k in w15]
    c = [float(k["c"]) for k in w15]
    v = [float(k.get("v", 0.0) or 0.0) for k in w15]
    atr = res.atr_pct(h, l, c)
    ax = res.adx(h, l, c)
    vol_mult = (v[-1] / (sum(v[-21:-1]) / 20.0)) if len(v) >= 21 and sum(v[-21:-1]) > 0 else None
    return {
        "atr_pct_15m": atr, "adx_15m": ax, "volume_multiple_15m": vol_mult,
        "regime": res.classify_regime(w1h),
    }


async def replay_symbol(client, symbol: str, *, limit_15m: int = 3000,
                        research: bool = True, ctx: dict | None = None) -> dict:
    try:
        return await _replay_symbol(client, symbol, limit_15m=limit_15m, research=research,
                                    ctx=ctx)
    except Exception as exc:  # reported per symbol; never silently dropped
        return {"symbol": symbol, "error": f"{type(exc).__name__}: {str(exc)[:200]}",
                "candidates": [], "rows": []}


# Fees and slippage scaled together; used for the break-even cost multiplier.
COST_GRID = (0.0, 0.05, 0.1, 0.15, 0.2, 0.3, 0.5, 0.75, 1.0, 1.5, 2.0)

# One exit-policy change at a time relative to the PRODUCTION-PARITY exit model.
PARITY_EXIT_VARIANTS = {
    "current": {},
    "no_partial_tp": {"enable_partial": False},
    "no_trailing": {"enable_trailing": False},
    "no_rr_double": {"enable_rr_double": False},
    "shifted_native_stops_legacy_bug": {"shift_native_stops": True},
}


def _parity_outcome(pctx: dict, *, direction, i, sig_entry, sl, tp, fee_rate, slip,
                    policy=None) -> dict | None:
    """Production-parity exit path + net R for one candidate geometry."""
    from bot import nexus_oos_execution_parity as xp
    k15, ts15 = pctx["k15"], pctx["ts15"]
    sim = xp.simulate_production_exit(
        direction=direction, bars=k15, start_idx=i, signal_entry=sig_entry,
        signal_sl=sl, signal_tp=tp, slippage_rate=slip,
        policy=policy or pctx["exit_policy"], ts_of=_ts_ms)
    if sim is None or not sim["legs"]:
        return None
    planned = abs(float(sig_entry) - float(sl)) / float(sig_entry)
    fts = pctx["funding_ts"]
    lo = bisect_right(fts, int(sim["entry_ts"]))
    hi = bisect_right(fts, int(sim["exit_ts"]))
    net = xp.legs_net_r(sim, direction=direction, fee_rate=fee_rate,
                        funding_events=pctx["funding_events"][lo:hi],
                        price_at_ts=lambda ts: _price_at_ts(k15, ts15, ts),
                        planned_risk_fraction=planned)
    if net.get("r") is None:
        return None
    net["sim"] = sim
    net["planned_risk_fraction"] = planned
    return net


async def _replay_symbol(client, symbol: str, *, limit_15m: int, research: bool,
                         ctx: dict | None = None) -> dict:
    from datetime import datetime, timezone
    from bot.strategy import Analyzer
    from bot.engine import TradingEngine
    from bot import nexus_ai
    from bot import nexus_oos_research as res
    from bot import nexus_oos_execution_parity as xp
    from bot.kucoin_execution_model import estimated_round_trip_cost_pct

    ctx = ctx or {}
    manifest = ctx.get("manifest")
    k15 = await fetch_history(client, symbol, "15", limit_15m)
    k1h = await fetch_history(client, symbol, "60", max(900, limit_15m // 4 + 120))
    k4h = await fetch_history(client, symbol, "240", max(300, limit_15m // 16 + 80))
    if len(k15) < 200 or len(k1h) < 60 or len(k4h) < 30:
        return {"symbol": symbol, "error": "insufficient_history",
                "candles_15m": len(k15), "candidates": [], "rows": []}

    start_ms = _ts_ms(k15[0])
    end_ms = _ts_ms(k15[-1]) + 15 * 60 * 1000
    funding_events = await fetch_public_funding_history(client, symbol, start_ms, end_ms)
    fee_rate = configured_taker_fee()
    slippage = slippage_rate_for_symbol(symbol)
    threshold = runtime_nexus_threshold(nexus_ai)

    ts15 = _timestamp_index(k15)
    ts1h = _timestamp_index(k1h)
    ts4h = _timestamp_index(k4h)
    analyzer = Analyzer()
    rows: list[CandidateOutcome] = []
    rich: list[dict] = []
    approved = rejected = analyzer_errors = 0
    counts = defaultdict(int)

    parity = manifest is not None
    if parity:
        exit_policy = manifest.exit_policy()
        leverage = int(manifest["LEVERAGE"])
        fee_mult = float(manifest["FEE_MULTIPLIER"])
        drift_bps = float(manifest["NEXUS_MAX_SIGNAL_DRIFT_BPS"])
        mmr = (ctx.get("mmr_proxy") or {}).get(symbol)
        cost_fraction = xp.production_cost_fraction(
            taker_fee=float(manifest["TAKER_FEE"]),
            expected_slippage_pct=float(manifest["NEXUS_EXPECTED_SLIPPAGE_PCT"]),
            modeled_round_trip_pct=estimated_round_trip_cost_pct(symbol, float(manifest["TAKER_FEE"])),
        )
        funding_sorted = sorted(funding_events, key=lambda e: int(e.get("timepoint", 0) or 0))
        pctx = {"k15": k15, "ts15": ts15, "funding_events": funding_sorted,
                "funding_ts": [int(e.get("timepoint", 0) or 0) for e in funding_sorted],
                "exit_policy": exit_policy}

    # Same decision window as the legacy replay (40 forward bars available), so
    # old-vs-new differences come from execution parity, not a different sample.
    for i in range(80, len(k15) - 40):
        decision_ts = ts15[i]
        w15 = _closed_window_by_ts(k15, ts15, decision_ts, 15, 80)
        w1h = _closed_window_by_ts(k1h, ts1h, decision_ts, 60, 50)
        w4h = _closed_window_by_ts(k4h, ts4h, decision_ts, 240, 30)
        if len(w15) < 60 or len(w1h) < 40 or len(w4h) < 20:
            continue
        try:
            sig = analyzer.analyze_mtf(
                symbol,
                w15,
                w1h,
                w4h,
                min_score=int(getattr(cfg, "MIN_ENTRY_SCORE", 65)),
                fee_mult=getattr(cfg, "FEE_MULTIPLIER", 2.0),
                vol_mult=getattr(cfg, "MIN_VOLUME_MULT", 1.2),
            )
        except Exception:
            analyzer_errors += 1
            continue
        if not sig:
            continue
        # Production has no replay-side R:R filter: the analyzer (with
        # rr_precision_hardening) already enforces MIN_RR_RATIO. Counted only.
        if float(sig.rr) < float(getattr(cfg, "MIN_RR_RATIO", 2.0)):
            counts["rr_below_min_after_analyzer"] += 1

        direction = str(sig.direction).upper()
        sim_args = dict(
            direction=direction,
            signal_entry=float(sig.entry),
            signal_sl=float(sig.sl),
            signal_tp=float(sig.tp),
            signal_tp1=float(getattr(sig, "tp1", sig.tp) or sig.tp),
            signal_tp2=float(getattr(sig, "tp2", sig.tp) or sig.tp),
            decision_idx=i,
            decision_ts=decision_ts,
            klines_15=k15,
            funding_events=funding_events,
            timestamps=ts15,
        )
        detail = {}
        r_legacy = _simulate_net_r(**sim_args, fee_rate=fee_rate, slippage_rate=slippage,
                                   detail=detail)
        legacy_ok = r_legacy is not None
        if not parity and not legacy_ok:
            continue
        ticker = {"lastPrice": str(float(k15[i]["o"]))}
        funding = _funding_at(funding_events, decision_ts)
        with _freeze_nexus_clock(nexus_ai, decision_ts):
            nx = _decide(nexus_ai, symbol, w15, w1h, w4h, sig, ticker, funding, threshold)
            approved_initial = getattr(nx, "execution_allowed", False) is True
            geometry = None
            approved_final = approved_initial
            if parity:
                geometry = xp.production_geometry(sig, leverage=leverage, mmr=mmr,
                                                  fee_multiplier=fee_mult)
                if geometry["status"] == "ADJUSTED" and approved_initial:
                    adj = xp._SigView(sig, sl=geometry["sl"], tp=geometry["tp"])
                    nx2 = _decide(nexus_ai, symbol, w15, w1h, w4h, adj, ticker, funding, threshold)
                    approved_final = getattr(nx2, "execution_allowed", False) is True
                    counts["geometry_adjusted_reapproved" if approved_final
                           else "geometry_adjusted_nexus_rejected"] += 1
            if research:
                open_nx = _decide(nexus_ai, symbol, w15, w1h, w4h, sig, ticker, funding,
                                  NON_BINDING_THRESHOLD)
                variants = {}
                for name in NEXUS_VARIANTS:
                    with _NexusVariant(nexus_ai, name):
                        d = _decide(nexus_ai, symbol, w15, w1h, w4h, sig, ticker, funding, threshold)
                    variants[name] = getattr(d, "execution_allowed", False) is True
                for name, cctx in CONTEXT_VARIANTS.items():
                    d = _decide(nexus_ai, symbol, w15, w1h, w4h, sig,
                                ticker if cctx["ticker"] else None,
                                funding if cctx["funding"] else None, threshold)
                    variants[name] = getattr(d, "execution_allowed", False) is True
        if legacy_ok and r_legacy is not None:
            approved += int(approved_initial)
            rejected += int(not approved_initial)
            confidence = max(0.0, min(1.0, float(getattr(nx, "confidence", 0.0) or 0.0) / 100.0))
            rows.append(CandidateOutcome(float(decision_ts), approved_initial, True, confidence,
                                         True, float(r_legacy)))

        if not research:
            continue
        hour = datetime.fromtimestamp(decision_ts / 1000, tz=timezone.utc)
        row = {
            "ts": int(decision_ts),
            "month": hour.strftime("%Y-%m"),
            "symbol": symbol,
            "direction": direction,
            "entry_type": str(getattr(sig, "entry_type", "UNKNOWN")),
            "strategy_score": int(getattr(sig, "score", 0) or 0),
            "rr": float(sig.rr),
            "approved_legacy": approved_initial,
            "r_legacy": float(r_legacy) if r_legacy is not None else None,
            "legacy_candidate": bool(legacy_ok and r_legacy is not None),
            "nexus_confidence": float(getattr(nx, "confidence", 0.0) or 0.0),
        }
        feats = _features(w15, w1h)
        nexus_score = getattr(open_nx, "setup_quality", None)
        gates_passed = getattr(open_nx, "execution_allowed", False) is True
        row.update({
            "gates_passed": gates_passed,
            "nexus_score": float(nexus_score) if gates_passed and nexus_score is not None else None,
            "nexus_regime": getattr(open_nx, "market_regime", None),
            "production_regime": getattr(open_nx, "market_regime", None) or "UNKNOWN",
            "research_regime": feats["regime"],
            "utc_hour_bucket": res.bucket(hour.hour, (6, 12, 18), ("00-05", "06-11", "12-17", "18-23")),
            "score_bucket": res.bucket(float(nexus_score) if gates_passed and nexus_score is not None else None,
                                       (55, 60, 65, 70, 75, 80, 85, 90)),
            "confidence_bucket": res.bucket(float(getattr(nx, "confidence", 0.0) or 0.0), (40, 50, 60, 70, 80)),
            "volatility_bucket": res.bucket(feats["atr_pct_15m"], (0.002, 0.004, 0.008)),
            "adx_bucket": res.bucket(feats["adx_15m"], (20, 25, 35, 50)),
            "volume_bucket": res.bucket(feats["volume_multiple_15m"], (0.5, 1.0, 1.5, 2.5)),
            "variants": variants,
        })
        if legacy_ok and r_legacy is not None:
            no_slip = _simulate_net_r(**sim_args, fee_rate=fee_rate, slippage_rate=0.0)
            row["legacy"] = {
                "r": float(r_legacy),
                "gross_r": detail.get("gross_r"), "fees_r": detail.get("fees_r"),
                "funding_r": detail.get("funding_r"),
                "slippage_r": (float(r_legacy) - float(no_slip)) if no_slip is not None else None,
                "outcome_end_ts": int(detail["exit_ts"]),
                "entry_fill": float(detail["entry_fill"]),
                "stop": float(detail["stop"]),
                "risk_fraction": float(detail["risk_fraction"]),
                "unshifted_native_r": _simulate_net_r(**sim_args, fee_rate=fee_rate,
                                                      slippage_rate=slippage, shift_native=False),
            }
            if approved_initial:
                row["legacy"]["_path"] = [
                    (_ts_ms(k15[j]) + 15 * 60 * 1000, float(k15[j]["c"]))
                    for j in range(i, len(k15))
                    if _ts_ms(k15[j]) + 15 * 60 * 1000 <= int(detail["exit_ts"])]

        if not parity:
            continue
        funnel = xp.funnel_flags(sig, decision_ts, TradingEngine)
        executable_px = adverse_fill(float(k15[i]["o"]), direction, is_entry=True,
                                     slippage_rate=slippage)
        drift = xp.drift_gate(float(sig.entry), executable_px, direction, drift_bps)
        final_sl = geometry["sl"] if geometry["status"] == "ADJUSTED" else float(sig.sl)
        final_tp = geometry["tp"] if geometry["status"] == "ADJUSTED" else float(sig.tp)
        executable = (geometry["status"] in ("SAFE", "ADJUSTED")
                      and funnel["regime_allows_direction"] and funnel["expected_pnl_positive"]
                      and funnel["adjusted_score"] >= int(manifest["MIN_ENTRY_SCORE"])
                      and not drift["blocked"])
        orig = _parity_outcome(pctx, direction=direction, i=i, sig_entry=float(sig.entry),
                               sl=float(sig.sl), tp=float(sig.tp), fee_rate=fee_rate, slip=slippage)
        final = orig if geometry["status"] != "ADJUSTED" else _parity_outcome(
            pctx, direction=direction, i=i, sig_entry=float(sig.entry), sl=final_sl, tp=final_tp,
            fee_rate=fee_rate, slip=slippage)
        if final is None:
            counts["parity_outcome_unavailable"] += 1
            executable = False
        row.update({
            "session": funnel["session"], "session_penalty": funnel["session_penalty"],
            "score_adjusted": funnel["adjusted_score"],
            "regime_allows_direction": funnel["regime_allows_direction"],
            "expected_pnl_positive": funnel["expected_pnl_positive"],
            "geometry_status": geometry["status"], "geometry_reason": geometry["reason"],
            "geometry_retained_fraction": geometry.get("retained_fraction"),
            "drift_bps": drift["signed_drift_bps"], "drift_blocked": drift["blocked"],
            "signal_entry": float(sig.entry), "sl": final_sl, "tp": final_tp,
            "sl_original": float(sig.sl), "tp_original": float(sig.tp),
            "executable": bool(executable),
            "approved": bool(executable and approved_final),
            "approved_final_nexus": bool(approved_final),
            "fee_rate": fee_rate, "cost_fraction": cost_fraction,
            "r_prod_original_geometry": orig["r"] if orig else None,
        })
        if final is not None:
            sim = final["sim"]
            row.update({
                "r": float(final["r"]),
                "gross_r": final["gross_r"], "fees_r": final["fees_r"],
                "funding_r": final["funding_r"],
                "fill": sim["fill"], "entry_fill": sim["fill"], "stop": final_sl,
                "risk_fraction": final["planned_risk_fraction"],
                "outcome_end_ts": int(sim["exit_ts"]),
                "exit_reason": sim["exit_reason"], "censored": sim["censored"],
                "exit_legs_n": len(sim["legs"]),
            })
            if row["executable"]:
                cost_r = {}
                for name, (fm, sm) in COST_SCENARIOS.items():
                    if name == "current":
                        cost_r[name] = float(final["r"])
                        continue
                    o = _parity_outcome(pctx, direction=direction, i=i, sig_entry=float(sig.entry),
                                        sl=final_sl, tp=final_tp, fee_rate=fee_rate * fm,
                                        slip=slippage * sm)
                    cost_r[name] = o["r"] if o else None
                no_slip_o = _parity_outcome(pctx, direction=direction, i=i, sig_entry=float(sig.entry),
                                            sl=final_sl, tp=final_tp, fee_rate=fee_rate, slip=0.0)
                exit_r = {}
                for name, kw in PARITY_EXIT_VARIANTS.items():
                    if name == "current":
                        exit_r[name] = float(final["r"])
                        continue
                    o = _parity_outcome(pctx, direction=direction, i=i, sig_entry=float(sig.entry),
                                        sl=final_sl, tp=final_tp, fee_rate=fee_rate, slip=slippage,
                                        policy=replace(exit_policy, **kw))
                    exit_r[name] = o["r"] if o else None
                cost_grid = {}
                for m in COST_GRID:
                    if m == 1.0:
                        cost_grid[str(m)] = float(final["r"])
                        continue
                    o = _parity_outcome(pctx, direction=direction, i=i, sig_entry=float(sig.entry),
                                        sl=final_sl, tp=final_tp, fee_rate=fee_rate * m,
                                        slip=slippage * m)
                    cost_grid[str(m)] = o["r"] if o else None
                row.update({
                    "cost_r": cost_r, "exit_r": exit_r, "cost_grid_r": cost_grid,
                    "slippage_r": (float(final["r"]) - no_slip_o["r"]) if no_slip_o else None,
                })
                if row["approved"]:
                    row["legs"] = [list(l) for l in sim["legs"]]
                    row["_marks"] = sim["marks"]
                    row["_funding"] = [
                        (int(e.get("timepoint", 0)), float(e.get("fundingRate", 0.0) or 0.0),
                         _price_at_ts(k15, ts15, int(e.get("timepoint", 0))))
                        for e in funding_events
                        if sim["entry_ts"] < int(e.get("timepoint", 0) or 0) <= sim["exit_ts"]]
        rich.append(row)

    return {
        "symbol": symbol,
        "candles_15m": len(k15),
        "requested_15m": limit_15m,
        "history_start_utc": datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc).isoformat(),
        "history_end_utc": datetime.fromtimestamp(end_ms / 1000, tz=timezone.utc).isoformat(),
        "history_days": (end_ms - start_ms) / 86_400_000,
        "history_shorter_than_requested": len(k15) < limit_15m,
        "funding_events": len(funding_events),
        "analyzer_errors": analyzer_errors,
        "approved": approved,
        "rejected": rejected,
        "nexus_threshold_used": threshold,
        "parity_counts": dict(sorted(counts.items())),
        "candidates": rows,
        "rows": rich,
        "historical_context": {
            "candles": True,
            "ticker_proxy": True,
            "funding_history": bool(funding_events),
            "historical_open_interest": False,
            "historical_orderbook": False,
            "parity_complete": False,
        },
    }


def _grid_break_even(rows: list[dict]) -> dict:
    """Break-even combined cost multiplier from the per-row cost grid."""
    pts = []
    for m in COST_GRID:
        vals = [r["cost_grid_r"][str(m)] for r in rows
                if (r.get("cost_grid_r") or {}).get(str(m)) is not None]
        if vals:
            pts.append((m, sum(vals) / len(vals)))
    if not pts:
        return {"multiplier": None, "note": "NO_DATA"}
    curve = {str(m): v for m, v in pts}
    if pts[0][1] <= 0:
        return {"multiplier": None, "note": "NEGATIVE_EVEN_AT_ZERO_COST",
                "expectancy_at_zero_cost": pts[0][1], "curve": curve}
    for (m0, f0), (m1, f1) in zip(pts, pts[1:]):
        if f0 > 0 >= f1:
            return {"multiplier": m0 + (m1 - m0) * f0 / (f0 - f1), "note": "CROSSING_FOUND",
                    "curve": curve}
    return {"multiplier": None, "note": f"POSITIVE_BEYOND_{pts[-1][0]:g}X_COST", "curve": curve}


def temporal_folds(exe: list[dict], folds: int = 4) -> dict:
    """Chronological folds of executable candidates (point estimates only;
    used by the gate as a count of positive folds, never as a CI)."""
    rows = sorted(exe, key=lambda r: r["ts"])
    out = []
    if len(rows) >= folds:
        for k in range(folds):
            part = rows[len(rows) * k // folds: len(rows) * (k + 1) // folds]
            appr = [float(r["r"]) for r in part if r.get("approved")]
            base = [float(r["r"]) for r in part]
            am = (sum(appr) / len(appr)) if appr else None
            bm = (sum(base) / len(base)) if base else None
            out.append({"fold": k + 1, "start_ts": part[0]["ts"], "end_ts": part[-1]["ts"],
                        "approved": len(appr), "approved_mean_r": am, "baseline_mean_r": bm,
                        "uplift_r": (am - bm) if am is not None and bm is not None else None})
    return {"folds": out,
            "folds_positive_approved_expectancy": sum(1 for f in out if (f["approved_mean_r"] or -1) > 0),
            "folds_positive_uplift": sum(1 for f in out if (f["uplift_r"] or -1) > 0)}


def research_status(infer: dict, approved_mean) -> str:
    appr = infer.get("approved_expectancy") or {}
    up = infer.get("uplift_vs_baseline") or {}
    lo, hi = appr.get("authority_ci_low"), appr.get("authority_ci_high")
    if approved_mean is None:
        return "INSUFFICIENT_EVIDENCE"
    if hi is not None and hi < 0:
        return "NEGATIVE_EXPECTANCY_ESTABLISHED"
    if approved_mean <= 0:
        return "NEGATIVE_POINT_ESTIMATE"
    if lo is not None and lo > 0 and (up.get("authority_ci_low") or -1) > 0:
        return "EDGE_SUPPORTED_BY_BLOCK_CI"
    return "EDGE_NOT_ESTABLISHED"


def _research_sections(all_rich: list[dict], threshold: float) -> dict:
    """CANDIDATE_RESEARCH on production-EXECUTABLE candidates with
    production-parity outcomes. Baseline = every executable strategy candidate
    (counterfactual execution); approved = final production approval."""
    from bot import nexus_oos_research as res
    from bot import nexus_oos_inference as inf
    from bot.nexus_probability import heuristic_win_probability

    exe = [r for r in all_rich if r.get("executable") and r.get("r") is not None]
    approved = [r for r in exe if r["approved"]]
    is_approved = lambda r: bool(r.get("approved"))  # noqa: E731
    out: dict = {"layer": "CANDIDATE_RESEARCH", "outcome_model": "PRODUCTION_PARITY_V1",
                 "population": "EXECUTABLE_CANDIDATES"}
    geometry = defaultdict(int)
    funnel_fail = defaultdict(int)
    for r in all_rich:
        geometry[str(r.get("geometry_status"))] += 1
        if not r.get("regime_allows_direction", True):
            funnel_fail["regime_disallows_direction"] += 1
        if not r.get("expected_pnl_positive", True):
            funnel_fail["expected_pnl_not_positive"] += 1
        if r.get("drift_blocked"):
            funnel_fail["pre_dispatch_drift"] += 1
        if (r.get("score_adjusted") is not None
                and r.get("score_adjusted") < (r.get("strategy_score") or 0)
                and not r.get("executable")):
            funnel_fail["session_penalty_involved"] += 1
    out["population_counts"] = {
        "strategy_candidates": len(all_rich),
        "executable_candidates": len(exe),
        "approved_production": len(approved),
        "approved_legacy_definition": sum(1 for r in all_rich if r.get("approved_legacy")),
        "geometry_status": dict(sorted(geometry.items())),
        "funnel_failures": dict(sorted(funnel_fail.items())),
        "censored_outcomes": sum(1 for r in exe if r.get("censored")),
    }
    out["performance"] = {
        "baseline": res.performance(exe),
        "approved": res.performance(approved),
        "rejected": res.performance([r for r in exe if not r["approved"]]),
        "approval_rate": (len(approved) / len(exe)) if exe else None,
        "rejection_rate": (1 - len(approved) / len(exe)) if exe else None,
    }
    out["inference"] = {
        "approved_expectancy": inf.dependence_aware_mean(exe, is_approved),
        "baseline_expectancy": inf.dependence_aware_mean(exe),
        "uplift_vs_baseline": inf.dependence_aware_diff(exe, is_approved, lambda r: True),
        "block_autocorrelation_24h": inf.lag1_block_autocorrelation(approved, inf.DEFAULT_BLOCK_MS),
        "dependence_diagnostics": inf.dependence_diagnostics(approved),
        "block_lengths_ms": list(inf.AUTHORITY_BLOCKS_MS),
        "authority_model": inf.AUTHORITY_MODEL,
        "iid_role": inf.IID_ROLE,
        "max_outcome_horizon_ms": max((r["outcome_end_ts"] - r["ts"] for r in exe), default=None),
    }
    out["effective_sample"] = {
        "raw_candidates": len(exe),
        "raw_approved": len(approved),
        "baseline": inf.effective_sample(exe),
        "approved": inf.effective_sample(exe, is_approved),
    }
    out["segments_approved"] = res.segments(approved)
    out["segments_baseline"] = res.segments(exe)
    out["concentration"] = {
        "approved_by_symbol": res.concentration(approved, "symbol"),
        "approved_by_month": res.concentration(approved, "month"),
        "approved_by_production_regime": res.concentration(approved, "production_regime"),
    }
    out["cost_stress_approved"] = res.cost_stress(approved)
    out["cost_stress_baseline"] = res.cost_stress(exe)
    out["break_even_cost_multiplier"] = {
        "approved": _grid_break_even(approved) if approved else None,
        "baseline": _grid_break_even(exe) if exe else None,
        "definition": "fees and slippage scaled together; 1.0 = current model; grid "
                      + ",".join(f"{m:g}" for m in COST_GRID),
    }
    out["exit_variants_approved"] = {
        name: res.compact([{"ts": r["ts"], "r": r["exit_r"][name]} for r in approved
                           if (r.get("exit_r") or {}).get(name) is not None])
        for name in PARITY_EXIT_VARIANTS
    }
    out["exit_reason_mix_approved"] = dict(sorted(
        __import__("collections").Counter(str(r.get("exit_reason")) for r in approved).items()))
    out["ablation"] = {name: res.paired_ablation(exe, name) for name in NEXUS_VARIANTS}
    out["context_ablation"] = {name: res.paired_ablation(exe, name) for name in CONTEXT_VARIANTS}
    out["ablation_notes"] = (
        "NEXUS variants are evaluated on the ORIGINAL signal geometry; the post-compression "
        "NEXUS re-run applies only to the production approval (approximation for ablations).")
    out["context_ablation_notes"] = {
        "C_candle_plus_available_derivatives": "identical to B: funding is the only derivatives input with public history",
        "OI": "no public historical OI in replay; never fabricated",
        "orderbook": "no historical order book; microstructure component always excluded",
        "news_and_market_risk": "live-only feeds; absent from replay",
    }
    out["strategy_gate_ablation"] = {"status": "NOT_ABLATED", **STRATEGY_GATES_NOT_ABLATED}
    out["threshold_research"] = res.threshold_research(
        exe, (55, 60, 65, 70, 75, 80, 85, 90), threshold)
    out["probability_calibration"] = res.calibration_report(exe, heuristic_win_probability)
    out["regime_parity"] = {
        "production_regime": "nexus_ai decision market_regime at each decision (primary)",
        "research_regime": "nexus_oos_research.classify_regime on closed 1h candles (diagnostic only)",
    }
    out["temporal_folds"] = temporal_folds(exe, folds=4)
    out["research_status"] = research_status(
        out["inference"], out["performance"]["approved"].get("net_expectancy_r"))
    return out


def _mean_n(vals) -> dict:
    vals = [float(v) for v in vals if v is not None]
    return {"n": len(vals), "mean_r": (sum(vals) / len(vals)) if vals else None}


def _parity_attribution(all_rich: list[dict]) -> dict:
    """Step-wise attribution of candidate-level expectancy on identical data.

    Each step adds exactly one parity correction to the previous one.
    """
    legacy_appr = [r for r in all_rich if r.get("legacy_candidate") and r.get("approved_legacy")]
    steps = [
        ("A0_legacy_model", "old exit model (shifted native stops, TP1==TP2 partial never, 40-bar exit)",
         _mean_n(r["r_legacy"] for r in legacy_appr)),
        ("A1_unshifted_native_stops", "A0 + exchange SL/TP at the unshifted signal levels",
         _mean_n((r.get("legacy") or {}).get("unshifted_native_r") for r in legacy_appr)),
        ("A2_production_exit_engine", "production exits (1R partial + break-even, trailing, 2R, "
         "native TP/SL, no time exit) on the original geometry",
         _mean_n(r.get("r_prod_original_geometry") for r in legacy_appr)),
        ("A3_cross_geometry", "A2 + post-NEXUS liquidation-safe geometry (BLOCK removed, "
         "ADJUSTED compressed and re-approved by NEXUS)",
         _mean_n(r.get("r") for r in legacy_appr
                 if r.get("geometry_status") in ("SAFE", "ADJUSTED") and r.get("approved_final_nexus"))),
        ("A4_production_funnel", "A3 + session score adjustment, regime direction, expected PnL "
         "and pre-dispatch drift gates (= production approval)",
         _mean_n(r.get("r") for r in all_rich if r.get("approved"))),
    ]
    out, prev = {}, None
    for key, what, m in steps:
        m = dict(m)
        m["change"] = what
        m["delta_vs_previous"] = (m["mean_r"] - prev) if (m["mean_r"] is not None and prev is not None) else None
        prev = m["mean_r"] if m["mean_r"] is not None else prev
        out[key] = m
    out["baseline_legacy"] = _mean_n(r["r_legacy"] for r in all_rich if r.get("legacy_candidate"))
    out["baseline_parity"] = _mean_n(r.get("r") for r in all_rich
                                     if r.get("executable") and r.get("r") is not None)
    return out


def _legacy_portfolio_rows(all_rich: list[dict]) -> list[dict]:
    rows = []
    for r in all_rich:
        lg = r.get("legacy") or {}
        if not (r.get("legacy_candidate") and r.get("approved_legacy") and lg.get("_path") is not None):
            continue
        rows.append({
            "ts": r["ts"], "symbol": r["symbol"], "direction": r["direction"], "approved": True,
            "r": lg["r"], "fees_r": lg.get("fees_r"), "slippage_r": lg.get("slippage_r"),
            "funding_r": lg.get("funding_r"), "entry_fill": lg["entry_fill"], "stop": lg["stop"],
            "risk_fraction": lg["risk_fraction"], "outcome_end_ts": lg["outcome_end_ts"],
            "_path": lg["_path"], "month": r.get("month"),
            "production_regime": r.get("production_regime"),
            "research_regime": r.get("research_regime"),
        })
    return rows


def _engine_rows(all_rich: list[dict]) -> list[dict]:
    out = []
    for r in all_rich:
        if not (r.get("approved") and r.get("executable") and r.get("legs")):
            continue
        row = dict(r)
        row["marks"] = r.get("_marks") or []
        row["funding"] = r.get("_funding") or []
        out.append(row)
    return out


def portfolio_policy():
    """LEGACY portfolio policy (pre-manifest), used only to re-run the legacy
    engine for old-vs-new attribution. The authoritative replay uses the
    pinned manifest (bot.nexus_oos_replay_manifest)."""
    import os as _os
    from bot import risk_policy as rp
    base = rp.load_policy(cfg)
    lev = float(_os.environ.get("OOS_PORTFOLIO_LEVERAGE", "50"))
    mdd = float(_os.environ.get("OOS_PORTFOLIO_MAX_DRAWDOWN", "0.50"))
    return rp.RiskPolicy(**{**base.__dict__, "leverage": lev, "max_drawdown": mdd,
                            "ignored_overrides": ()})


def _summ(rep: dict) -> dict:
    keys = ("ending_equity", "net_return", "total_trades", "portfolio_max_drawdown",
            "net_expectancy_r", "profit_factor", "win_rate", "blocked_daily_stop",
            "blocked_drawdown", "daily_stop_days")
    return {k: rep.get(k) for k in keys}


def closed_candle_sentinel_installed() -> bool:
    from bot.strategy import Analyzer
    return bool(getattr(Analyzer, "_timestamp_closed_candle_integrity_installed", False))


CANONICAL_BLOCKER_KEYS = (
    "HISTORICAL_CONTEXT_PARITY_INCOMPLETE", "APPROVED_EXPECTANCY_NOT_POSITIVE",
    "APPROVED_EXPECTANCY_BLOCK_CI_NOT_POSITIVE", "UPLIFT_BLOCK_CI_NOT_POSITIVE",
    "SYMBOLS_UNAVAILABLE", "PORTFOLIO_PARITY_INCOMPLETE", "PRETRADE_CONTEXT_PARITY_INCOMPLETE",
    "NO_CANDIDATES", "CONTRACT_METADATA_UNAVAILABLE",
)


async def run_real_replay(symbols: Iterable[str], *, limit_15m: int = 3000,
                          research: bool = True, manifest_path: str | None = None) -> dict:
    # Install the production wrapper stack in a PAPER-safe process. No exchange
    # mutation methods are called by this replay.
    from bot.runtime_bootstrap import install as install_runtime
    from bot import nexus_ai
    from bot import nexus_oos_inference as inf
    from bot import nexus_oos_execution_parity as xp
    from bot import nexus_oos_replay_manifest as rm
    from bot.nexus_oos_robustness import analyze_robustness

    symbols = list(symbols)
    manifest = None
    if research:
        # Fail closed BEFORE touching the runtime or the network.
        manifest = rm.load(manifest_path)
    install_runtime()
    if manifest is not None:
        manifest.verify_runtime()   # values production reads in THIS process

    symbol_reports = []
    all_rows: list[CandidateOutcome] = []
    all_rich: list[dict] = []
    contracts = None
    instruments: dict = {}
    mmr_proxy: dict = {}
    async with PublicKuCoinFuturesClient() as client:
        if research:
            try:
                contracts = await client._get("/api/v1/contracts/active")
            except Exception:  # reported via CONTRACT_METADATA_UNAVAILABLE
                contracts = None
            alias = {"XBT": "BTC"}
            for c in contracts or []:
                if not isinstance(c, dict) or not str(c.get("symbol", "")).endswith("USDTM"):
                    continue
                base = alias.get(str(c.get("baseCurrency", "")), str(c.get("baseCurrency", "")))
                std = f"{base}USDT"
                if std in symbols and std not in instruments:
                    info = xp.instrument_from_public_contract(c)
                    instruments[std] = info
                    if "contractMaintainMarginReference" in info:
                        mmr_proxy[std] = info["contractMaintainMarginReference"]
        ctx = {"manifest": manifest, "mmr_proxy": mmr_proxy}
        for symbol in symbols:
            rep = await replay_symbol(client, symbol, limit_15m=limit_15m, research=research,
                                      ctx=ctx)
            symbol_reports.append(rep)
            all_rows.extend(rep.get("candidates", []))
            all_rich.extend(rep.get("rows", []))

    # ── LEGACY_DIAGNOSTIC: the pre-parity IID edge report. No authority. ──
    edge = build_edge_report(all_rows)
    legacy_ok, legacy_blockers = edge_promotion_decision(edge)
    context_parity_complete = all(
        bool(rep.get("historical_context", {}).get("parity_complete"))
        for rep in symbol_reports if "error" not in rep
    ) and bool(symbol_reports)

    compact_symbols = []
    for rep in symbol_reports:
        compact = {k: v for k, v in rep.items() if k not in ("candidates", "rows")}
        compact["candidate_count"] = len(rep.get("candidates", []))
        compact_symbols.append(compact)

    blockers: list[str] = []
    if not context_parity_complete:
        blockers.append("HISTORICAL_CONTEXT_PARITY_INCOMPLETE")
    if any(rep.get("error") for rep in symbol_reports):
        blockers.append("SYMBOLS_UNAVAILABLE")

    report = asdict(edge)
    report["authority"] = "LEGACY_DIAGNOSTIC_ONLY"
    artifact = {
        "result_kind": "RESEARCH_RESULT",
        "authority_model": inf.AUTHORITY_MODEL,
        "promotion_gate": "python -m bot.nexus_oos_promotion_gate <artifact>",
        "report": report,
        "legacy_diagnostic": {
            "authority": "NONE",
            "note": ("Pre-parity IID edge report on the legacy exit model and legacy approval "
                     "definition. Kept for comparison; never read by the promotion gate."),
            "status": "AI_EDGE_PROVEN" if legacy_ok else "AI_EDGE_NOT_PROVEN",
            "blockers": list(legacy_blockers),
            "report": asdict(edge),
            "robustness": analyze_robustness(
                [rep for rep in symbol_reports if not rep.get("error")], temporal_folds=4),
        },
        "symbols": compact_symbols,
        "requested_symbols": symbols,
        "unavailable_symbols": {rep["symbol"]: rep["error"] for rep in symbol_reports if rep.get("error")},
        "nexus_threshold_used": runtime_nexus_threshold(nexus_ai),
        "historical_context_parity_complete": context_parity_complete,
        "methodology": {
            "closed_candles_only": True,
            "closed_candle_sentinel_verified": closed_candle_sentinel_installed(),
            "historical_clock_frozen": True,
            "same_bar_ambiguity": "STOP_FIRST",
            "fees_included": True,
            "slippage_included": True,
            "funding_included_when_public_history_available": True,
            "exchange_mutations": False,
            "runtime_policy_mutations": False,
            "exit_model": "PRODUCTION_PARITY_V1 (see replay_parity.exit_parity_matrix)",
            "legacy_exit_model": "SL, TP1 50% + break-even, TP2, 40-bar time exit (legacy_diagnostic only)",
        },
    }
    if research and manifest is not None:
        ex, pre = xp.exit_parity_status(), xp.pretrade_parity_status()
        artifact["replay_policy_manifest"] = manifest.report()
        artifact["replay_parity"] = {
            "exit_parity_matrix": list(xp.EXIT_PARITY_MATRIX),
            "pretrade_gate_matrix": list(xp.PRETRADE_GATE_MATRIX),
            "exit_parity_complete": ex["complete"],
            "exit_parity_blocking_rules": ex["blocking_rules"],
            "pretrade_parity_complete": pre["complete"],
            "pretrade_parity_blocking_gates": pre["blocking_rules"],
            "portfolio_parity_complete": bool(ex["complete"] and pre["complete"]),
            "tp1_equals_tp2_root_cause": (
                "strategy.calc_sl_tp (distinct TP1/TP2) has no caller; analyze_mtf and the "
                "adaptive-MTF wrapper build Signal without tp1/tp2 and Signal.__post_init__ sets "
                "tp1 = tp2 = tp. Production reality, not a replay artefact. Production partial TP "
                "uses 1R (+0.03% funding buffer), not sig.tp1."),
        }
        if not ex["complete"] or not pre["complete"]:
            blockers.append("PORTFOLIO_PARITY_INCOMPLETE")
        if not pre["complete"]:
            blockers.append("PRETRADE_CONTEXT_PARITY_INCOMPLETE")
    if research and all_rich and manifest is not None:
        from bot import nexus_oos_portfolio_engine as pe
        from bot import nexus_oos_portfolio_replay as legacy_pr
        cand = _research_sections(all_rich, runtime_nexus_threshold(nexus_ai))
        artifact["candidate_research"] = cand
        artifact["research_status"] = cand["research_status"]
        artifact["parity_attribution"] = _parity_attribution(all_rich)
        if not instruments:
            blockers.append("CONTRACT_METADATA_UNAVAILABLE")
        rows = _engine_rows(all_rich)
        port = pe.run_portfolio(rows, manifest, instruments=instruments, mmr_proxy=mmr_proxy)
        port["path_bootstrap"] = pe.path_bootstrap_authority(
            rows, manifest, instruments=instruments, mmr_proxy=mmr_proxy, replicates=200)
        port["robustness"] = {
            "authority_ci_low": port["path_bootstrap"]["authority_ci_low"],
            "authority_ci_high": port["path_bootstrap"]["authority_ci_high"],
            "authority_metric": "net_return",
            "method": "path bootstrap (see path_bootstrap)",
        }
        port["daily_pnl_semantics_sensitivity"] = {
            sem: _summ(pe.run_portfolio(rows, manifest, instruments=instruments, mmr_proxy=mmr_proxy,
                                        daily_pnl_semantics=sem))
            for sem in ("PRODUCTION_REALIZED_TODAY_PLUS_OPEN_UNREALIZED",
                        "EQUITY_ANCHORED_AT_UTC_MIDNIGHT")}
        port["gate_attribution"] = {
            name: _summ(pe.run_portfolio(rows, manifest, instruments=instruments, mmr_proxy=mmr_proxy,
                                         toggles=replace(pe.Toggles(), **{name: False})))
            for name in pe.Toggles.__dataclass_fields__}
        port["contract_spec_sensitivity"] = pe.contract_spec_sensitivity(
            rows, manifest, instruments=instruments, mmr_proxy=mmr_proxy)
        artifact["portfolio_replay"] = port
        lrules, lmmr = legacy_pr.contract_rules_from_public(contracts or [], symbols)
        legacy_port = legacy_pr.run_portfolio_legacy(
            _legacy_portfolio_rows(all_rich), portfolio_policy(), contract_rules=lrules or None,
            mmr=lmmr or None)
        artifact["portfolio_replay_legacy"] = {
            **_summ(legacy_port), "authority": "NONE",
            "note": "legacy engine + legacy rows on the same data; for attribution only"}
        infer = cand["inference"]
        appr_mean = cand["performance"]["approved"].get("net_expectancy_r")
        if appr_mean is None or appr_mean <= 0:
            blockers.append("APPROVED_EXPECTANCY_NOT_POSITIVE")
        lo = infer["approved_expectancy"].get("authority_ci_low")
        if lo is None or lo <= 0:
            blockers.append("APPROVED_EXPECTANCY_BLOCK_CI_NOT_POSITIVE")
        ulo = infer["uplift_vs_baseline"].get("authority_ci_low")
        if ulo is None or ulo <= 0:
            blockers.append("UPLIFT_BLOCK_CI_NOT_POSITIVE")
    elif research:
        blockers.append("NO_CANDIDATES")
    artifact["blockers"] = sorted(set(blockers))
    artifact["status"] = "AI_EDGE_PROVEN" if not artifact["blockers"] else "AI_EDGE_NOT_PROVEN"
    return artifact


def _strip_private(obj):
    if isinstance(obj, dict):
        return {k: _strip_private(v) for k, v in obj.items() if not str(k).startswith("_")}
    if isinstance(obj, list):
        return [_strip_private(v) for v in obj]
    return obj


def main(argv=None) -> int:
    """RESEARCH command: exits 0 whenever the replay ran, even on negative
    evidence. Promotion is decided separately by bot.nexus_oos_promotion_gate.
    A missing / malformed / mismatched replay policy manifest exits 2 (fail
    closed) before any market data is read."""
    from bot.nexus_oos_replay_manifest import ManifestError
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", nargs="+", default=["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT"])
    parser.add_argument("--limit-15m", type=int, default=3000)
    parser.add_argument("--output", default="artifacts/nexus_oos_real_replay.json")
    parser.add_argument("--policy-manifest", default=None,
                        help="Pinned replay policy (default research/replay_policy_manifest.json).")
    parser.add_argument("--no-research", action="store_true",
                        help="Skip research sections (faster; no promotion evidence).")
    args = parser.parse_args(argv)
    try:
        report = asyncio.run(run_real_replay(args.symbols, limit_15m=args.limit_15m,
                                             research=not args.no_research,
                                             manifest_path=args.policy_manifest))
    except ManifestError as exc:
        print(json.dumps({"status": "REPLAY_POLICY_MANIFEST_INVALID", "error": str(exc)}))
        return 2
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(_strip_private(report), indent=2, sort_keys=True, default=str),
                   encoding="utf-8")
    cand = report.get("candidate_research") or {}
    perf = cand.get("performance") or {}
    infer = cand.get("inference") or {}
    port = report.get("portfolio_replay") or {}
    print(json.dumps({
        "status": report["status"],
        "research_status": report.get("research_status"),
        "authority_model": report.get("authority_model"),
        "blockers": report["blockers"],
        "unavailable_symbols": report["unavailable_symbols"],
        "nexus_threshold_used": report["nexus_threshold_used"],
        "policy_sha256": (report.get("replay_policy_manifest") or {}).get("policy_sha256"),
        "executable_candidates": (perf.get("baseline") or {}).get("trades"),
        "approved_candidates": (perf.get("approved") or {}).get("trades"),
        "approved_expectancy_r": (perf.get("approved") or {}).get("net_expectancy_r"),
        "approved_block_authority_ci": [
            (infer.get("approved_expectancy") or {}).get("authority_ci_low"),
            (infer.get("approved_expectancy") or {}).get("authority_ci_high")],
        "uplift_block_authority_ci": [
            (infer.get("uplift_vs_baseline") or {}).get("authority_ci_low"),
            (infer.get("uplift_vs_baseline") or {}).get("authority_ci_high")],
        "portfolio_ending_equity": port.get("ending_equity"),
        "output": str(out),
    }, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
