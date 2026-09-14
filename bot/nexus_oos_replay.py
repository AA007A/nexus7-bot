"""Leakage-safe historical replay builder for NEXUS incremental-edge evidence.

Analytics only: no exchange mutations, no release/risk/sizing changes.

The replay starts from baseline trades produced by ``bot.backtest``. For every
baseline candidate it reconstructs only candles that were closed at the original
decision timestamp, reruns the production strategy to recover the exact signal
geometry, evaluates NEXUS on clock-normalized copies of those historical windows,
and attaches the already simulated net outcome to both approved and rejected
candidates.

Optional historical microstructure/derivatives inputs are intentionally absent in
this first evidence path unless a future caller supplies a parity-capable decision
function. Therefore the returned diagnostics label the dataset CORE_CANDLES_ONLY;
it must not be represented as full production parity.
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from math import isfinite
import time
from typing import Callable, Iterable

from bot.config import cfg
from bot.nexus_oos_edge_gate import CandidateOutcome


DecisionFn = Callable[..., object]


@dataclass(frozen=True)
class ReplayEvidence:
    symbol: str
    candidates: tuple[CandidateOutcome, ...]
    baseline_trade_count: int
    evaluated_count: int
    warmup_excluded_count: int
    parity: str = "CORE_CANDLES_ONLY"


def _ts_ms(candle: dict) -> int:
    ts = int(candle.get("ts", 0) or 0)
    if ts < 1e11:
        ts *= 1000
    return ts


def _index(candles: list) -> list[int]:
    return [_ts_ms(c) for c in candles]


def _closed_window(candles: list, timestamps: list[int], decision_ts_ms: int,
                   interval_min: int, lookback: int) -> list:
    latest_closed_open = int(decision_ts_ms) - int(interval_min) * 60 * 1000
    end = bisect_right(timestamps, latest_closed_open)
    start = max(0, end - int(lookback))
    return candles[start:end]


def _shift_clock(candles: list, offset_ms: int) -> list:
    out: list[dict] = []
    for candle in candles:
        copied = dict(candle)
        if copied.get("ts") is not None:
            copied["ts"] = _ts_ms(copied) + int(offset_ms)
        out.append(copied)
    return out


def clock_normalized_windows(k15: list, k1h: list, k4h: list,
                             decision_ts_ms: int, *,
                             reference_now_ms: int | None = None) -> tuple[list, list, list]:
    """Shift only timestamps, preserving all relative chronology and OHLCV.

    ``nexus_ai.validate_data`` compares the latest candle timestamp with wall
    clock time. Historical candles would otherwise be rejected merely because
    they are old today, rather than stale *at the replay decision*. A constant
    timestamp translation preserves spacing/alignment while making the replay's
    decision instant equivalent to ``reference_now_ms``.
    """
    ref = int(reference_now_ms if reference_now_ms is not None else time.time() * 1000)
    offset = ref - int(decision_ts_ms)
    return (
        _shift_clock(k15, offset),
        _shift_clock(k1h, offset),
        _shift_clock(k4h, offset),
    )


def _confidence01(decision: object) -> float:
    raw = float(getattr(decision, "confidence", 0.0) or 0.0)
    if raw > 1.0:
        raw /= 100.0
    return max(0.0, min(1.0, raw))


def _candidate_r_multiple(trade: dict, signal_entry: float, signal_stop: float) -> float:
    entry = float(signal_entry)
    stop = float(signal_stop)
    pnl_fraction = float(trade.get("pnl_pct", 0.0) or 0.0)
    if not all(isfinite(v) for v in (entry, stop, pnl_fraction)) or entry <= 0.0:
        raise ValueError("invalid replay trade geometry")
    market_entry = float(trade.get("entry_fill", entry) or entry)
    risk_fraction = abs(entry - stop) / market_entry
    if not isfinite(risk_fraction) or risk_fraction <= 0.0:
        raise ValueError("replay trade has zero/invalid initial risk")
    return pnl_fraction / risk_fraction


def build_nexus_oos_candidates(
    symbol: str,
    klines_15: list,
    klines_1h: list,
    klines_4h: list,
    baseline_trades: Iterable[dict],
    *,
    strategy_min_score: int = 75,
    strategy_min_rr: float = 2.0,
    nexus_min_score: float | None = None,
    decision_fn: DecisionFn | None = None,
    reference_now_ms: int | None = None,
) -> ReplayEvidence:
    """Build approved+rejected counterfactual outcomes from baseline trades.

    No future candles enter NEXUS. Outcome information is attached only *after*
    the decision is computed from the closed historical windows.
    """
    from bot.nexus_ai import decide as production_decide
    from bot.strategy import Analyzer

    decide = decision_fn or production_decide
    analyzer = Analyzer()
    ts15, ts1h, ts4h = _index(klines_15), _index(klines_1h), _index(klines_4h)
    rows: list[CandidateOutcome] = []
    warmup_excluded = 0
    evaluated = 0
    trades = list(baseline_trades)

    for trade in trades:
        decision_ts = int(trade.get("ts", 0) or 0)
        if decision_ts <= 0:
            raise ValueError("baseline trade missing decision timestamp")

        # NEXUS production minimum history is stricter than the legacy strategy.
        k15 = _closed_window(klines_15, ts15, decision_ts, 15, 60)
        k1h = _closed_window(klines_1h, ts1h, decision_ts, 60, 40)
        k4h = _closed_window(klines_4h, ts4h, decision_ts, 240, 20)
        if len(k15) < 60 or len(k1h) < 40 or len(k4h) < 20:
            warmup_excluded += 1
            rows.append(CandidateOutcome(float(decision_ts), False, False, 0.0, True, 0.0))
            continue

        sig = analyzer.analyze_mtf(
            symbol,
            k15,
            k1h,
            k4h,
            min_score=int(strategy_min_score),
            fee_mult=getattr(cfg, "FEE_MULTIPLIER", 2.0),
            vol_mult=getattr(cfg, "MIN_VOLUME_MULT", 1.2),
        )
        if not sig or float(sig.rr) < float(strategy_min_rr):
            raise RuntimeError(
                f"baseline replay mismatch at ts={decision_ts}: strategy signal not reproducible"
            )

        nk15, nk1h, nk4h = clock_normalized_windows(
            k15, k1h, k4h, decision_ts,
            reference_now_ms=reference_now_ms,
        )
        decision = decide(
            symbol,
            nk15,
            nk1h,
            nk4h,
            entry=float(sig.entry),
            sl=float(sig.sl),
            tp=float(sig.tp),
            min_score=nexus_min_score,
        )
        approved = bool(getattr(decision, "execution_allowed", False))
        confidence = _confidence01(decision)
        r_multiple = _candidate_r_multiple(trade, float(sig.entry), float(sig.sl))
        rows.append(
            CandidateOutcome(
                timestamp=float(decision_ts),
                approved=approved,
                baseline_eligible=True,
                confidence=confidence,
                outcome_known=True,
                r_multiple=float(r_multiple),
            ).validate()
        )
        evaluated += 1

    return ReplayEvidence(
        symbol=str(symbol),
        candidates=tuple(rows),
        baseline_trade_count=len(trades),
        evaluated_count=evaluated,
        warmup_excluded_count=warmup_excluded,
    )


def run_nexus_oos_replay(
    symbol: str,
    klines_15: list,
    klines_1h: list,
    klines_4h: list,
    *,
    strategy_min_score: int = 75,
    strategy_min_rr: float = 2.0,
    execution_context: dict | None = None,
    nexus_min_score: float | None = None,
    decision_fn: DecisionFn | None = None,
    reference_now_ms: int | None = None,
) -> ReplayEvidence:
    """Generate baseline trades with the hardened backtest then evaluate NEXUS."""
    from bot.backtest import run_strategy_public

    baseline = run_strategy_public(
        klines_15,
        klines_1h,
        klines_4h,
        min_score=int(strategy_min_score),
        min_rr=float(strategy_min_rr),
        symbol=symbol,
        execution_context=execution_context,
    )
    return build_nexus_oos_candidates(
        symbol,
        klines_15,
        klines_1h,
        klines_4h,
        baseline,
        strategy_min_score=strategy_min_score,
        strategy_min_rr=strategy_min_rr,
        nexus_min_score=nexus_min_score,
        decision_fn=decision_fn,
        reference_now_ms=reference_now_ms,
    )
