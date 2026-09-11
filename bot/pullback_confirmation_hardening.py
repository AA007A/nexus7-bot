"""Require lower-timeframe reversal evidence before emitting PULLBACK entries.

A higher-timeframe trend can legitimately contain a 15m counter-trend pullback,
but the previous strategy could emit a PULLBACK candidate while 15m momentum
and structure were still actively moving against the intended trade. That
created repeated low-quality candidates that NEXUS then rejected at the
ensemble/MTF gate.

This runtime guard does not loosen NEXUS. It makes the strategy wait for a
closed-candle reversal confirmation before a PULLBACK signal is allowed to
continue downstream. BOS_BREAK and MOMENTUM entries are unchanged.
"""
from __future__ import annotations

from typing import Any

from bot.indicators import ema, macd, rsi, smc_analysis


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _direction(value: Any) -> str:
    return str(getattr(value, "value", value)).upper()


def _confirmed_15m(k15: list) -> list:
    data = list(k15 or [])
    if len(data) > 20:
        return data[:-1]
    return data


def _pullback_metrics(k15: list, direction: str) -> dict:
    data = _confirmed_15m(k15)
    if len(data) < 30:
        return {"ok": False, "reason": "insufficient_15m_history"}

    closes = [_safe_float(k.get("c")) for k in data]
    opens = [_safe_float(k.get("o")) for k in data]
    highs = [_safe_float(k.get("h")) for k in data]
    lows = [_safe_float(k.get("l")) for k in data]

    c0, c1 = closes[-1], closes[-2]
    o0 = opens[-1]
    e9 = float(ema(closes, 9)[-1])
    e20 = float(ema(closes, 20)[-1])
    r = float(rsi(closes)[-1])
    _, _, hist = macd(closes)
    h0 = float(hist[-1])
    h1 = float(hist[-2])
    smc = smc_analysis(highs, lows, closes)

    d = _direction(direction)
    if d == "LONG":
        body_aligned = c0 > o0
        progress = c0 > c1
        fast_reclaim = c0 >= e9
        macd_turn = h0 > h1
        rsi_ok = r >= 40.0
        opposite_structure = str(smc.get("structure", "UNKNOWN")).upper() == "DOWNTREND"
        opposite_bos = bool(smc.get("bos")) and str(smc.get("bos_dir", "NONE")).upper() == "BEARISH"
        aligned_bos = bool(smc.get("bos")) and str(smc.get("bos_dir", "NONE")).upper() == "BULLISH"
        ema20_side = c0 >= e20
    elif d == "SHORT":
        body_aligned = c0 < o0
        progress = c0 < c1
        fast_reclaim = c0 <= e9
        macd_turn = h0 < h1
        rsi_ok = r <= 60.0
        opposite_structure = str(smc.get("structure", "UNKNOWN")).upper() == "UPTREND"
        opposite_bos = bool(smc.get("bos")) and str(smc.get("bos_dir", "NONE")).upper() == "BULLISH"
        aligned_bos = bool(smc.get("bos")) and str(smc.get("bos_dir", "NONE")).upper() == "BEARISH"
        ema20_side = c0 <= e20
    else:
        return {"ok": False, "reason": "invalid_direction"}

    votes = {
        "body_aligned": body_aligned,
        "price_progress": progress,
        "ema9_reclaim": fast_reclaim,
        "macd_turn": macd_turn,
        "rsi_recovered": rsi_ok,
    }
    vote_count = sum(bool(v) for v in votes.values())

    # Explicit opposite BOS is always a veto. A still-opposite local structure
    # is also a veto unless an aligned BOS has just printed, which is precisely
    # the structural reversal evidence needed for a pullback re-entry.
    if opposite_bos:
        passed = False
        reason = "opposite_bos"
    elif opposite_structure and not aligned_bos:
        passed = False
        reason = "opposite_structure_not_reversed"
    else:
        # Require broad micro confirmation, not a single indicator flip. EMA20
        # is diagnostic rather than mandatory so an early but genuine reclaim
        # can still qualify after 4/5 independent reversal votes.
        passed = vote_count >= 4
        reason = "confirmed" if passed else "insufficient_reversal_votes"

    return {
        "ok": passed,
        "reason": reason,
        "votes": votes,
        "vote_count": vote_count,
        "structure": str(smc.get("structure", "UNKNOWN")),
        "bos": bool(smc.get("bos", False)),
        "bos_dir": str(smc.get("bos_dir", "NONE")),
        "choch": bool(smc.get("choch", False)),
        "rsi": round(r, 2),
        "macd_hist": round(h0, 8),
        "macd_prev": round(h1, 8),
        "close": c0,
        "ema9": round(e9, 8),
        "ema20": round(e20, 8),
        "ema20_side": bool(ema20_side),
    }


def install(Analyzer, log) -> None:
    if getattr(Analyzer, "_pullback_confirmation_installed", False):
        return

    original = Analyzer.analyze_mtf

    def analyze_mtf_confirmed_pullback(self, symbol, k15, k1h, k4h, *args, **kwargs):
        signal = original(self, symbol, k15, k1h, k4h, *args, **kwargs)
        if signal is None or str(getattr(signal, "entry_type", "")).upper() != "PULLBACK":
            return signal

        metrics = _pullback_metrics(k15, getattr(signal, "direction", ""))
        if not metrics.get("ok"):
            log.info(
                "[PULLBACK_CONFIRMATION] symbol=%s side=%s result=BLOCKED "
                "reason=%s votes=%s vote_count=%s structure=%s bos=%s bos_dir=%s "
                "rsi=%s macd=%s/%s ema20_side=%s execution_effect=SIGNAL_FILTER_ONLY",
                symbol,
                getattr(signal, "direction", "UNKNOWN"),
                metrics.get("reason"),
                metrics.get("votes", {}),
                metrics.get("vote_count", 0),
                metrics.get("structure", "UNKNOWN"),
                metrics.get("bos", False),
                metrics.get("bos_dir", "NONE"),
                metrics.get("rsi", "N/A"),
                metrics.get("macd_hist", "N/A"),
                metrics.get("macd_prev", "N/A"),
                metrics.get("ema20_side", "N/A"),
            )
            return None

        log.info(
            "[PULLBACK_CONFIRMATION] symbol=%s side=%s result=PASS "
            "votes=%s vote_count=%s structure=%s bos=%s bos_dir=%s rsi=%s "
            "execution_effect=SIGNAL_FILTER_ONLY",
            symbol,
            getattr(signal, "direction", "UNKNOWN"),
            metrics.get("votes", {}),
            metrics.get("vote_count", 0),
            metrics.get("structure", "UNKNOWN"),
            metrics.get("bos", False),
            metrics.get("bos_dir", "NONE"),
            metrics.get("rsi", "N/A"),
        )
        return signal

    Analyzer.analyze_mtf = analyze_mtf_confirmed_pullback
    Analyzer._pullback_confirmation_installed = True
    log.warning(
        "[PULLBACK_CONFIRMATION] installed closed_15m_reversal_required=true "
        "min_votes=4/5 opposite_structure_requires_aligned_bos=true "
        "opposite_bos=BLOCK BOS_BREAK_and_MOMENTUM_unchanged=true "
        "NEXUS_and_risk_gates_unchanged=true"
    )
