"""Require reversal evidence before emitting PULLBACK entries.

The canonical path remains closed-candle and requires >=4/5 reversal votes.
To reduce avoidable latency, a blocked PULLBACK may use the current 15m bar only
as a stricter acceleration path: 5/5 votes, price on the correct EMA20 side,
and no opposite structure/BOS. BOS_BREAK and MOMENTUM remain unchanged.

The intrabar path never bypasses NEXUS, risk, ownership, market-risk, sizing,
liquidation, TPSL, idempotency or final pre-dispatch market checks.
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
    """Drop the disposable/current final bar used by the strategy stack."""
    data = list(k15 or [])
    return data[:-1] if len(data) > 20 else data


def _metrics_from_data(data: list, direction: str) -> dict:
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

    if opposite_bos:
        passed = False
        reason = "opposite_bos"
    elif opposite_structure and not aligned_bos:
        passed = False
        reason = "opposite_structure_not_reversed"
    else:
        passed = vote_count >= 4
        reason = "confirmed" if passed else "insufficient_reversal_votes"

    return {
        "ok": passed,
        "reason": reason,
        "votes": votes,
        "vote_count": vote_count,
        "structure": str(smc.get("structure", "UNKNOWN")),
        "opposite_structure": bool(opposite_structure),
        "opposite_bos": bool(opposite_bos),
        "aligned_bos": bool(aligned_bos),
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


def _pullback_metrics(k15: list, direction: str) -> dict:
    return _metrics_from_data(_confirmed_15m(k15), direction)


def _intrabar_fast_metrics(k15: list, direction: str) -> dict:
    """Use current 15m bar only for a stricter 5/5 acceleration decision."""
    data = list(k15 or [])
    # A current/disposable bar must actually exist in addition to enough history.
    if len(data) < 31:
        return {"ok": False, "reason": "insufficient_intrabar_history"}
    metrics = _metrics_from_data(data, direction)
    if not metrics.get("ok"):
        return metrics

    strict = (
        int(metrics.get("vote_count", 0)) == 5
        and bool(metrics.get("ema20_side"))
        and not bool(metrics.get("opposite_structure"))
        and not bool(metrics.get("opposite_bos"))
    )
    out = dict(metrics)
    out["ok"] = strict
    out["reason"] = "intrabar_5of5_confirmed" if strict else "intrabar_not_strict_enough"
    return out


def install(Analyzer, log) -> None:
    if getattr(Analyzer, "_pullback_confirmation_installed", False):
        return

    original = Analyzer.analyze_mtf

    def analyze_mtf_confirmed_pullback(self, symbol, k15, k1h, k4h, *args, **kwargs):
        signal = original(self, symbol, k15, k1h, k4h, *args, **kwargs)
        if signal is None or str(getattr(signal, "entry_type", "")).upper() != "PULLBACK":
            return signal

        direction = getattr(signal, "direction", "")
        metrics = _pullback_metrics(k15, direction)
        if metrics.get("ok"):
            log.info(
                "[PULLBACK_CONFIRMATION] symbol=%s side=%s result=PASS path=closed_15m "
                "votes=%s vote_count=%s structure=%s bos=%s bos_dir=%s rsi=%s "
                "execution_effect=SIGNAL_FILTER_ONLY",
                symbol, direction, metrics.get("votes", {}), metrics.get("vote_count", 0),
                metrics.get("structure", "UNKNOWN"), metrics.get("bos", False),
                metrics.get("bos_dir", "NONE"), metrics.get("rsi", "N/A"),
            )
            return signal

        # Acceleration is intentionally narrower than the canonical 4/5 rule.
        # Never use it to bypass an explicit opposite structure/BOS diagnosis.
        fast = {"ok": False, "reason": "not_eligible"}
        if metrics.get("reason") == "insufficient_reversal_votes":
            fast = _intrabar_fast_metrics(k15, direction)
            if fast.get("ok"):
                log.warning(
                    "[PULLBACK_CONFIRMATION] symbol=%s side=%s result=PASS path=intrabar_fast "
                    "closed_votes=%s intrabar_votes=%s ema20_side=true structure=%s "
                    "strict=5/5 NEXUS_and_risk_gates_preserved=true execution_effect=SIGNAL_FILTER_ONLY",
                    symbol, direction, metrics.get("vote_count", 0), fast.get("vote_count", 0),
                    fast.get("structure", "UNKNOWN"),
                )
                return signal

        log.info(
            "[PULLBACK_CONFIRMATION] symbol=%s side=%s result=BLOCKED reason=%s "
            "votes=%s vote_count=%s structure=%s bos=%s bos_dir=%s rsi=%s "
            "macd=%s/%s ema20_side=%s intrabar_reason=%s intrabar_votes=%s "
            "execution_effect=SIGNAL_FILTER_ONLY",
            symbol,
            direction or "UNKNOWN",
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
            fast.get("reason", "not_evaluated"),
            fast.get("vote_count", 0),
        )
        return None

    Analyzer.analyze_mtf = analyze_mtf_confirmed_pullback
    Analyzer._pullback_confirmation_installed = True
    log.warning(
        "[PULLBACK_CONFIRMATION] installed closed_15m_min_votes=4/5 "
        "intrabar_fast_path=5/5_plus_ema20_no_opposite_structure_or_bos "
        "opposite_structure_requires_aligned_bos=true opposite_bos=BLOCK "
        "BOS_BREAK_and_MOMENTUM_unchanged=true NEXUS_and_risk_gates_unchanged=true"
    )
