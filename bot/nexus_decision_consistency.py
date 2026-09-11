"""Closed-candle parity and score telemetry for the NEXUS decision layer.

The canonical strategy explicitly removes the still-forming last candle before
4H/1H/15M analysis. Historically ``nexus_ai.decide`` received the raw caches,
so the strategy and the final AI gate could evaluate different market states in
the same scan. This module makes the decision layer consume the same confirmed
candle semantics without changing any execution threshold.

It also emits a read-only score decomposition for every terminal NEXUS decision
so rejected opportunities can be distinguished from legitimate safety vetoes.
No exchange mutation or execution permission is introduced here.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, Tuple


_MIN_CONFIRMED = {"15m": 60, "1h": 40, "4h": 20}


def _closed_view(klines: Iterable[dict] | None, timeframe: str) -> list:
    """Return a copy ending at the last confirmed candle.

    Runtime market-data caches contain the currently forming candle as the final
    element. We remove it when doing so still preserves the NEXUS minimum sample
    for that timeframe. This matches the strategy's anti-repaint intent while
    remaining fail-closed on short histories.
    """
    data = list(klines or [])
    minimum = _MIN_CONFIRMED[timeframe]
    if len(data) > minimum:
        return data[:-1]
    return data


def closed_mtf(k15, k1h, k4h) -> Tuple[list, list, list]:
    return (
        _closed_view(k15, "15m"),
        _closed_view(k1h, "1h"),
        _closed_view(k4h, "4h"),
    )


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _score_snapshot(nexus_ai, *, symbol: str, k15: list, k1h: list, k4h: list,
                    entry: float, sl: float, tp: float, funding=None,
                    oi_delta=None, ls_ratio=None, news_score=None,
                    ticker=None, oi=None, orderbook=None) -> Dict[str, Any]:
    """Recompute deterministic score ingredients for observability only."""
    try:
        closes = [float(k["c"]) for k in k15]
        highs = [float(k["h"]) for k in k15]
        lows = [float(k["l"]) for k in k15]
        volumes = [float(k.get("v", 0)) for k in k15]
        if not closes:
            return {"ok": False, "reason": "no_closed_15m"}

        dq = nexus_ai.validate_data(
            symbol, k15, k1h, k4h, ticker=ticker, funding=funding,
            oi=oi, orderbook=orderbook,
        )
        regime, _ = nexus_ai.detect_regime(closes, highs, lows, volumes)
        mtf = nexus_ai.analyze_mtf(k15, k1h, k4h)
        models = nexus_ai.run_ensemble(
            closes, highs, lows, volumes,
            funding=funding, oi_delta=oi_delta, ls_ratio=ls_ratio,
        )
        fusion = nexus_ai._fuse(models, regime)
        direction = fusion.get("direction")
        ev = nexus_ai.expected_value(
            _safe_float(0.30 + (_safe_float(fusion.get("confidence")) / 100.0) * 0.45),
            entry, sl, tp,
        )
        if direction is None or not ev.get("valid"):
            return {
                "ok": True,
                "regime": getattr(regime, "value", str(regime)),
                "mtf": mtf.get("detail", ""),
                "fusion_direction": getattr(direction, "value", str(direction)),
                "fusion_confidence": _safe_float(fusion.get("confidence")),
                "fusion_risk": _safe_float(fusion.get("risk")),
                "data_quality": _safe_float(getattr(dq, "score", 0.0)),
                "ev_valid": bool(ev.get("valid")),
                "rr_net": _safe_float(ev.get("rr_net")),
                "ev_pct": _safe_float(ev.get("ev_pct")),
            }

        sc = nexus_ai._score_components(models, mtf, ev["rr_net"], direction, regime)
        component_score = _safe_float(sc.get("total"))
        risk_penalty = _safe_float(fusion.get("risk")) * 0.20
        after_risk = max(0.0, component_score - risk_penalty)

        news_adjustment = 0.0
        if news_score is not None:
            news_value = _safe_float(news_score)
            direction_value = getattr(direction, "value", str(direction))
            aligned = (
                (direction_value == "LONG" and news_value > 0)
                or (direction_value == "SHORT" and news_value < 0)
            )
            adjustment = min(5.0, abs(news_value) / 20.0)
            news_adjustment = adjustment if aligned else -adjustment

        after_news = after_risk + news_adjustment
        data_quality = _safe_float(getattr(dq, "score", 0.0))
        estimated_final = round(max(0.0, min(100.0, after_news * data_quality / 100.0)), 2)
        return {
            "ok": True,
            "regime": getattr(regime, "value", str(regime)),
            "mtf": mtf.get("detail", ""),
            "fusion_direction": getattr(direction, "value", str(direction)),
            "fusion_confidence": _safe_float(fusion.get("confidence")),
            "fusion_risk": _safe_float(fusion.get("risk")),
            "components": sc.get("components", {}),
            "unavailable": sc.get("unavailable", []),
            "component_score": round(component_score, 2),
            "risk_penalty": round(risk_penalty, 2),
            "news_adjustment": round(news_adjustment, 2),
            "data_quality": round(data_quality, 2),
            "estimated_final": estimated_final,
            "rr_net": _safe_float(ev.get("rr_net")),
            "ev_pct": _safe_float(ev.get("ev_pct")),
        }
    except Exception as exc:
        return {"ok": False, "reason": type(exc).__name__}


def install(nexus_ai, log) -> None:
    if getattr(nexus_ai, "_closed_candle_consistency_installed", False):
        return

    original = nexus_ai.decide

    def decide_closed_candles(symbol: str, k15: list, k1h: list, k4h: list, *args, **kwargs):
        c15, c1h, c4h = closed_mtf(k15, k1h, k4h)
        decision = original(symbol, c15, c1h, c4h, *args, **kwargs)

        entry = _safe_float(kwargs.get("entry", args[0] if len(args) > 0 else 0.0))
        sl = _safe_float(kwargs.get("sl", args[1] if len(args) > 1 else 0.0))
        tp = _safe_float(kwargs.get("tp", args[2] if len(args) > 2 else 0.0))
        snapshot = _score_snapshot(
            nexus_ai,
            symbol=symbol,
            k15=c15, k1h=c1h, k4h=c4h,
            entry=entry, sl=sl, tp=tp,
            funding=kwargs.get("funding"),
            oi_delta=kwargs.get("oi_delta"),
            ls_ratio=kwargs.get("ls_ratio"),
            news_score=kwargs.get("news_score"),
            ticker=kwargs.get("ticker"),
            oi=kwargs.get("oi"),
            orderbook=kwargs.get("orderbook"),
        )
        log.info(
            "[NEXUS_SCORE_DECOMP] symbol=%s decision=%s final=%.2f "
            "estimated_final=%s component_score=%s risk_penalty=%s "
            "dq=%s news_adj=%s confidence=%s rr_net=%s ev=%s regime=%s "
            "mtf=%s components=%s unavailable=%s closed_candles=15m:%d,1h:%d,4h:%d "
            "execution_effect=NONE",
            symbol,
            getattr(decision, "decision", "UNKNOWN"),
            _safe_float(getattr(decision, "setup_quality", 0.0)),
            snapshot.get("estimated_final", "N/A"),
            snapshot.get("component_score", "N/A"),
            snapshot.get("risk_penalty", "N/A"),
            snapshot.get("data_quality", "N/A"),
            snapshot.get("news_adjustment", "N/A"),
            snapshot.get("fusion_confidence", "N/A"),
            snapshot.get("rr_net", "N/A"),
            snapshot.get("ev_pct", "N/A"),
            snapshot.get("regime", "N/A"),
            snapshot.get("mtf", "N/A"),
            snapshot.get("components", {}),
            snapshot.get("unavailable", []),
            len(c15), len(c1h), len(c4h),
        )
        return decision

    nexus_ai.decide = decide_closed_candles
    nexus_ai._closed_candle_consistency_installed = True
    log.warning(
        "[NEXUS_CLOSED_CANDLE_PARITY] installed strategy_ai_same_confirmed_candle_semantics=true "
        "score_decomposition=true thresholds_unchanged=true execution_effect=NONE"
    )
