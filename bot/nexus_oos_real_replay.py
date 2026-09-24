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
from dataclasses import asdict
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
) -> float | None:
    """Mirror KUCOIN_MARKET_PROXY_V1 and return net R for one candidate.

    Defaults reproduce the historical model exactly. The optional exit-policy
    parameters exist for one-at-a-time exit research: ``partial`` (TP1 50%),
    ``break_even`` (SL to entry after TP1), ``max_bars`` (time exit) and
    ``stagnation_bars`` (exit at close if TP1 not reached by then). ``detail``,
    when given, receives gross/fees/funding components in R.
    """
    market_open = float(klines_15[decision_idx]["o"])
    entry_fill = adverse_fill(market_open, direction, is_entry=True, slippage_rate=slippage_rate)
    if entry_fill <= 0 or signal_entry <= 0:
        return None

    delta = entry_fill - signal_entry
    sl = float(signal_sl) + delta
    tp = float(signal_tp) + delta
    tp1 = float(signal_tp1) + delta
    tp2 = float(signal_tp2) + delta
    risk_fraction = abs(entry_fill - sl) / entry_fill
    if risk_fraction <= 0:
        return None

    timestamps = _timestamp_index(klines_15)
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
                        research: bool = True) -> dict:
    try:
        return await _replay_symbol(client, symbol, limit_15m=limit_15m, research=research)
    except Exception as exc:  # reported per symbol; never silently dropped
        return {"symbol": symbol, "error": f"{type(exc).__name__}: {str(exc)[:200]}",
                "candidates": [], "rows": []}


async def _replay_symbol(client, symbol: str, *, limit_15m: int, research: bool) -> dict:
    from datetime import datetime, timezone
    from bot.strategy import Analyzer
    from bot import nexus_ai
    from bot import nexus_oos_research as res

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
        if not sig or float(sig.rr) < float(getattr(cfg, "MIN_RR_RATIO", 2.0)):
            continue

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
        )
        detail: dict = {}
        r_multiple = _simulate_net_r(**sim_args, fee_rate=fee_rate, slippage_rate=slippage,
                                     detail=detail)
        if r_multiple is None:
            continue

        ticker = {"lastPrice": str(float(k15[i]["o"]))}
        funding = _funding_at(funding_events, decision_ts)
        with _freeze_nexus_clock(nexus_ai, decision_ts):
            nx = _decide(nexus_ai, symbol, w15, w1h, w4h, sig, ticker, funding, threshold)
            is_approved = getattr(nx, "execution_allowed", False) is True
            if research:
                open_nx = _decide(nexus_ai, symbol, w15, w1h, w4h, sig, ticker, funding,
                                  NON_BINDING_THRESHOLD)
                variants = {}
                for name in NEXUS_VARIANTS:
                    with _NexusVariant(nexus_ai, name):
                        d = _decide(nexus_ai, symbol, w15, w1h, w4h, sig, ticker, funding, threshold)
                    variants[name] = getattr(d, "execution_allowed", False) is True
                for name, ctx in CONTEXT_VARIANTS.items():
                    d = _decide(nexus_ai, symbol, w15, w1h, w4h, sig,
                                ticker if ctx["ticker"] else None,
                                funding if ctx["funding"] else None, threshold)
                    variants[name] = getattr(d, "execution_allowed", False) is True
        approved += int(is_approved)
        rejected += int(not is_approved)
        confidence = max(0.0, min(1.0, float(getattr(nx, "confidence", 0.0) or 0.0) / 100.0))
        rows.append(CandidateOutcome(float(decision_ts), is_approved, True, confidence, True, float(r_multiple)))

        if not research:
            continue
        no_slip = _simulate_net_r(**sim_args, fee_rate=fee_rate, slippage_rate=0.0)
        cost_r = {}
        for name, (fm, sm) in COST_SCENARIOS.items():
            cost_r[name] = (r_multiple if name == "current" else
                            _simulate_net_r(**sim_args, fee_rate=fee_rate * fm, slippage_rate=slippage * sm))
        exit_r = {}
        for name, kwargs in EXIT_VARIANTS.items():
            exit_r[name] = (r_multiple if name == "current" else
                            _simulate_net_r(**sim_args, fee_rate=fee_rate, slippage_rate=slippage, **kwargs))
        feats = _features(w15, w1h)
        nexus_score = getattr(open_nx, "setup_quality", None)
        gates_passed = getattr(open_nx, "execution_allowed", False) is True
        hour = datetime.fromtimestamp(decision_ts / 1000, tz=timezone.utc)
        rich.append({
            "ts": int(decision_ts),
            "month": hour.strftime("%Y-%m"),
            "symbol": symbol,
            "direction": direction,
            "entry_type": str(getattr(sig, "entry_type", "UNKNOWN")),
            "strategy_score": int(getattr(sig, "score", 0) or 0),
            "r": float(r_multiple),
            "gross_r": detail.get("gross_r"),
            "fees_r": detail.get("fees_r"),
            "funding_r": detail.get("funding_r"),
            "slippage_r": (float(r_multiple) - float(no_slip)) if no_slip is not None else None,
            "approved": is_approved,
            "gates_passed": gates_passed,
            "nexus_score": float(nexus_score) if gates_passed and nexus_score is not None else None,
            "nexus_regime": getattr(open_nx, "market_regime", None),  # threshold CHOPPY bump
            "nexus_confidence": float(getattr(nx, "confidence", 0.0) or 0.0),
            "production_regime": getattr(open_nx, "market_regime", None) or "UNKNOWN",
            "research_regime": feats["regime"],
            "outcome_end_ts": int(detail["exit_ts"]),
            "entry_fill": float(detail["entry_fill"]),
            "stop": float(detail["stop"]),
            "risk_fraction": float(detail["risk_fraction"]),
            "_path": [(_ts_ms(k15[j]) + 15 * 60 * 1000, float(k15[j]["c"]))
                      for j in range(i, len(k15))
                      if _ts_ms(k15[j]) + 15 * 60 * 1000 <= int(detail["exit_ts"])],
            "utc_hour_bucket": res.bucket(hour.hour, (6, 12, 18), ("00-05", "06-11", "12-17", "18-23")),
            "score_bucket": res.bucket(float(nexus_score) if gates_passed and nexus_score is not None else None,
                                       (55, 60, 65, 70, 75, 80, 85, 90)),
            "confidence_bucket": res.bucket(float(getattr(nx, "confidence", 0.0) or 0.0), (40, 50, 60, 70, 80)),
            "volatility_bucket": res.bucket(feats["atr_pct_15m"], (0.002, 0.004, 0.008)),
            "adx_bucket": res.bucket(feats["adx_15m"], (20, 25, 35, 50)),
            "volume_bucket": res.bucket(feats["volume_multiple_15m"], (0.5, 1.0, 1.5, 2.5)),
            "variants": variants,
            "cost_r": cost_r,
            "exit_r": exit_r,
            "_sim": (sim_args, fee_rate, slippage),
        })

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


def _research_sections(all_rich: list[dict], threshold: float) -> dict:
    """CANDIDATE_RESEARCH: does the signal-selection layer have edge?"""
    from bot import nexus_oos_research as res
    from bot import nexus_oos_inference as inf
    from bot.nexus_probability import heuristic_win_probability

    approved = [r for r in all_rich if r["approved"]]
    is_approved = lambda r: bool(r.get("approved"))  # noqa: E731
    out: dict = {"layer": "CANDIDATE_RESEARCH"}
    out["performance"] = {
        "baseline": res.performance(all_rich),
        "approved": res.performance(approved),
        "rejected": res.performance([r for r in all_rich if not r["approved"]]),
        "approval_rate": (len(approved) / len(all_rich)) if all_rich else None,
        "rejection_rate": (1 - len(approved) / len(all_rich)) if all_rich else None,
    }
    out["inference"] = {
        "approved_expectancy": inf.dependence_aware_mean(all_rich, is_approved),
        "baseline_expectancy": inf.dependence_aware_mean(all_rich),
        "uplift_vs_baseline": inf.dependence_aware_diff(all_rich, is_approved, lambda r: True),
        "block_autocorrelation_24h": inf.lag1_block_autocorrelation(approved, inf.DEFAULT_BLOCK_MS),
        "block_ms": inf.DEFAULT_BLOCK_MS,
        "sensitivity_block_ms": inf.SENSITIVITY_BLOCK_MS,
        "max_outcome_horizon_ms": max((r["outcome_end_ts"] - r["ts"] for r in all_rich), default=None),
    }
    out["effective_sample"] = {
        "raw_candidates": len(all_rich),
        "raw_approved": len(approved),
        "baseline": inf.effective_sample(all_rich),
        "approved": inf.effective_sample(all_rich, is_approved),
    }
    out["segments_approved"] = res.segments(approved)
    out["segments_baseline"] = res.segments(all_rich)
    out["concentration"] = {
        "approved_by_symbol": res.concentration(approved, "symbol"),
        "approved_by_month": res.concentration(approved, "month"),
        "approved_by_production_regime": res.concentration(approved, "production_regime"),
    }
    out["cost_stress_approved"] = res.cost_stress(approved)
    out["cost_stress_baseline"] = res.cost_stress(all_rich)

    def _exp_at(rows, mult):
        vals = []
        for r in rows:
            sim_args, fee, slip = r["_sim"]
            v = _simulate_net_r(**sim_args, fee_rate=fee * mult, slippage_rate=slip * mult)
            if v is not None:
                vals.append(v)
        return sum(vals) / len(vals) if vals else float("-inf")

    out["break_even_cost_multiplier"] = {
        "approved": res.break_even_multiplier(lambda m: _exp_at(approved, m)) if approved else None,
        "baseline": res.break_even_multiplier(lambda m: _exp_at(all_rich, m)) if all_rich else None,
        "definition": "fees and slippage scaled together; 1.0 = current model",
    }
    out["exit_variants_approved"] = {
        name: res.compact([{"ts": r["ts"], "r": r["exit_r"][name]} for r in approved
                           if r["exit_r"].get(name) is not None])
        for name in EXIT_VARIANTS
    }
    out["exit_variants_not_modeled"] = list(EXITS_NOT_MODELED)
    out["ablation"] = {name: res.paired_ablation(all_rich, name) for name in NEXUS_VARIANTS}
    out["context_ablation"] = {name: res.paired_ablation(all_rich, name) for name in CONTEXT_VARIANTS}
    out["context_ablation_notes"] = {
        "C_candle_plus_available_derivatives": "identical to B: funding is the only derivatives input with public history",
        "OI": "no public historical OI in replay; never fabricated",
        "orderbook": "no historical order book; microstructure component always excluded",
        "news_and_market_risk": "live-only feeds; absent from replay",
    }
    out["strategy_gate_ablation"] = {"status": "NOT_ABLATED", **STRATEGY_GATES_NOT_ABLATED}
    out["threshold_research"] = res.threshold_research(
        all_rich, (55, 60, 65, 70, 75, 80, 85, 90), threshold)
    out["probability_calibration"] = res.calibration_report(all_rich, heuristic_win_probability)
    out["regime_parity"] = {
        "production_regime": "nexus_ai decision market_regime at each decision (primary)",
        "research_regime": "nexus_oos_research.classify_regime on closed 1h candles (diagnostic only)",
    }
    return out


def portfolio_policy():
    """Risk policy for the portfolio replay: canonical code defaults with the
    production-reported leverage (50x) and MAX_DRAWDOWN (50%). MAX_RISK_PCT,
    MAX_MARGIN_PCT, MAX_POSITIONS and daily stop use the canonical config."""
    import os as _os
    from bot import risk_policy as rp
    base = rp.load_policy(cfg)
    lev = float(_os.environ.get("OOS_PORTFOLIO_LEVERAGE", "50"))
    mdd = float(_os.environ.get("OOS_PORTFOLIO_MAX_DRAWDOWN", "0.50"))
    return rp.RiskPolicy(**{**base.__dict__, "leverage": lev, "max_drawdown": mdd,
                            "ignored_overrides": ()})


async def run_real_replay(symbols: Iterable[str], *, limit_15m: int = 3000,
                          research: bool = True) -> dict:
    # Install the production wrapper stack in a PAPER-safe process. No exchange
    # mutation methods are called by this replay.
    from bot.runtime_bootstrap import install as install_runtime
    from bot import nexus_ai
    from bot.nexus_oos_robustness import analyze_robustness
    install_runtime()

    symbol_reports = []
    all_rows: list[CandidateOutcome] = []
    all_rich: list[dict] = []
    contracts = None
    async with PublicKuCoinFuturesClient() as client:
        if research:
            try:
                contracts = await client._get("/api/v1/contracts/active")
            except Exception:  # reported via contract_metadata=FINE_LOT_FALLBACK
                contracts = None
        for symbol in symbols:
            rep = await replay_symbol(client, symbol, limit_15m=limit_15m, research=research)
            symbol_reports.append(rep)
            all_rows.extend(rep.get("candidates", []))
            all_rich.extend(rep.get("rows", []))

    edge = build_edge_report(all_rows)
    statistically_ok, blockers = edge_promotion_decision(edge)
    context_parity_complete = all(
        bool(rep.get("historical_context", {}).get("parity_complete"))
        for rep in symbol_reports if "error" not in rep
    ) and bool(symbol_reports)
    final_blockers = list(blockers)
    if not context_parity_complete:
        final_blockers.append("HISTORICAL_CONTEXT_PARITY_INCOMPLETE")
    if edge.nexus_expectancy_r is None or edge.nexus_expectancy_r <= 0:
        final_blockers.append("APPROVED_EXPECTANCY_NOT_POSITIVE")
    if any(rep.get("error") for rep in symbol_reports):
        final_blockers.append("SYMBOLS_UNAVAILABLE")
    status = "AI_EDGE_PROVEN" if not final_blockers else "AI_EDGE_NOT_PROVEN"

    compact_symbols = []
    for rep in symbol_reports:
        compact = {k: v for k, v in rep.items() if k not in ("candidates", "rows")}
        compact["candidate_count"] = len(rep.get("candidates", []))
        compact_symbols.append(compact)

    artifact = {
        "status": status,
        "result_kind": "RESEARCH_RESULT",
        "promotion_gate": "python -m bot.nexus_oos_promotion_gate <artifact>",
        "blockers": sorted(set(final_blockers)),
        "report": asdict(edge),
        "symbols": compact_symbols,
        "requested_symbols": list(symbols),
        "unavailable_symbols": {rep["symbol"]: rep["error"] for rep in symbol_reports if rep.get("error")},
        "nexus_threshold_used": runtime_nexus_threshold(nexus_ai),
        "historical_context_parity_complete": context_parity_complete,
        "robustness": analyze_robustness(
            [rep for rep in symbol_reports if not rep.get("error")], temporal_folds=4),
        "methodology": {
            "closed_candles_only": True,
            "historical_clock_frozen": True,
            "same_bar_ambiguity": "STOP_FIRST",
            "fees_included": True,
            "slippage_included": True,
            "funding_included_when_public_history_available": True,
            "exchange_mutations": False,
            "runtime_policy_mutations": False,
            "exit_model": "SL, TP1 50% + break-even, TP2, 40-bar time exit",
            "pretrade_score_gate_modeled": False,
        },
    }
    if research and all_rich:
        from bot import nexus_oos_portfolio_replay as pr
        artifact["candidate_research"] = _research_sections(all_rich, runtime_nexus_threshold(nexus_ai))
        rules, mmr = pr.contract_rules_from_public(contracts or [], list(symbols))
        artifact["portfolio_replay"] = pr.run_portfolio(
            all_rich, portfolio_policy(), contract_rules=rules or None, mmr=mmr or None)
        artifact["portfolio_replay"]["contract_metadata_symbols"] = sorted(rules)
        infer = artifact["candidate_research"]["inference"]
        extra = []
        lo = infer["approved_expectancy"].get("authority_ci_low")
        if lo is None or lo <= 0:
            extra.append("APPROVED_EXPECTANCY_BLOCK_CI_NOT_POSITIVE")
        ulo = infer["uplift_vs_baseline"].get("authority_ci_low")
        if ulo is None or ulo <= 0:
            extra.append("UPLIFT_BLOCK_CI_NOT_POSITIVE")
        if extra:
            artifact["blockers"] = sorted(set(artifact["blockers"]) | set(extra))
            artifact["status"] = "AI_EDGE_NOT_PROVEN"
    elif research:
        artifact["blockers"] = sorted(set(artifact["blockers"]) | {"NO_CANDIDATES"})
        artifact["status"] = "AI_EDGE_NOT_PROVEN"
    return artifact


def _strip_private(obj):
    if isinstance(obj, dict):
        return {k: _strip_private(v) for k, v in obj.items() if not str(k).startswith("_")}
    if isinstance(obj, list):
        return [_strip_private(v) for v in obj]
    return obj


def main() -> int:
    """RESEARCH command: exits 0 whenever the replay ran, even on negative
    evidence. Promotion is decided separately by bot.nexus_oos_promotion_gate."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", nargs="+", default=["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT"])
    parser.add_argument("--limit-15m", type=int, default=3000)
    parser.add_argument("--output", default="artifacts/nexus_oos_real_replay.json")
    parser.add_argument("--no-research", action="store_true",
                        help="Skip research sections (faster; no promotion evidence).")
    args = parser.parse_args()
    report = asyncio.run(run_real_replay(args.symbols, limit_15m=args.limit_15m,
                                         research=not args.no_research))
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(_strip_private(report), indent=2, sort_keys=True, default=str),
                   encoding="utf-8")
    print(json.dumps({
        "status": report["status"],
        "blockers": report["blockers"],
        "unavailable_symbols": report["unavailable_symbols"],
        "nexus_threshold_used": report["nexus_threshold_used"],
        "baseline_candidates": report["report"]["baseline_candidates"],
        "approved_candidates": report["report"]["approved_candidates"],
        "rejected_candidates": report["report"]["rejected_candidates"],
        "baseline_expectancy_r": report["report"]["baseline_expectancy_r"],
        "nexus_expectancy_r": report["report"]["nexus_expectancy_r"],
        "expectancy_uplift_r": report["report"]["expectancy_uplift_r"],
        "bootstrap_ci_low_r": report["report"]["bootstrap_ci_low_r"],
        "bootstrap_ci_high_r": report["report"]["bootstrap_ci_high_r"],
        "output": str(out),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
