"""Leverage-compatible SL/TP geometry before NEXUS evaluation.

At high configured leverage a technically valid ATR stop can sit beyond the
exchange liquidation boundary. The engine correctly fails closed in that case,
but rejecting only at the final execution boundary wastes otherwise valid
setups and makes a fixed high-leverage configuration operationally inert.

This adapter runs on the final strategy signal *before* NEXUS, RiskManagerV3
and execution. When the strategy stop is wider than the conservative maximum
safe stop for the configured leverage, it may compress the stop and target
symmetrically while preserving the original R:R. It never changes leverage,
direction, score or any execution permission.

Safety invariants:
- liquidation guard remains authoritative and re-checks the final signal;
- an extra 0.10 percentage-point liquidation headroom is reserved;
- geometry is rejected instead of compressed when more than 50% of the
  original stop distance would be removed;
- fee viability is re-checked after compression;
- any malformed/non-finite geometry fails closed.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


_EXTRA_LIQ_HEADROOM_PCT = 0.10
_MIN_RETAINED_STOP_FRACTION = 0.50


@dataclass(frozen=True)
class GeometryResult:
    allowed: bool
    adjusted: bool
    sl: float
    tp: float
    rr: float
    original_stop_pct: float
    final_stop_pct: float
    safe_stop_pct: float
    retained_fraction: float
    reason: str


def _safe_stop_pct(liquidation, *, entry: float, leverage: int,
                   is_long: bool, symbol: str) -> float:
    mmr, _official = liquidation.get_mmr(symbol)
    liq_price = liquidation.liquidation_price(
        entry, leverage, is_long, mmr
    )
    if not all(math.isfinite(v) and v > 0 for v in (entry, liq_price)):
        return 0.0
    liq_move_pct = abs(entry - liq_price) / entry * 100.0
    return max(
        0.0,
        liq_move_pct
        - float(liquidation.MIN_GAP_PCT)
        - _EXTRA_LIQ_HEADROOM_PCT,
    )


def adapt_levels(liquidation, *, symbol: str, direction: str, entry: float,
                 sl: float, tp: float, leverage: int) -> GeometryResult:
    """Return leverage-compatible geometry or a fail-closed rejection."""
    values = (entry, sl, tp)
    if direction not in {"LONG", "SHORT"}:
        return GeometryResult(False, False, sl, tp, 0.0, 0.0, 0.0, 0.0, 0.0,
                              "invalid_direction")
    if leverage <= 0 or not all(math.isfinite(v) and v > 0 for v in values):
        return GeometryResult(False, False, sl, tp, 0.0, 0.0, 0.0, 0.0, 0.0,
                              "invalid_numeric_geometry")
    if direction == "LONG" and not (sl < entry < tp):
        return GeometryResult(False, False, sl, tp, 0.0, 0.0, 0.0, 0.0, 0.0,
                              "invalid_long_geometry")
    if direction == "SHORT" and not (tp < entry < sl):
        return GeometryResult(False, False, sl, tp, 0.0, 0.0, 0.0, 0.0, 0.0,
                              "invalid_short_geometry")

    raw_risk = abs(entry - sl)
    raw_reward = abs(tp - entry)
    if raw_risk <= 0 or raw_reward <= 0:
        return GeometryResult(False, False, sl, tp, 0.0, 0.0, 0.0, 0.0, 0.0,
                              "zero_risk_or_reward")
    rr = raw_reward / raw_risk
    original_stop_pct = raw_risk / entry * 100.0
    safe_stop_pct = _safe_stop_pct(
        liquidation,
        entry=entry,
        leverage=leverage,
        is_long=(direction == "LONG"),
        symbol=symbol,
    )
    if safe_stop_pct <= 0 or not math.isfinite(safe_stop_pct):
        return GeometryResult(False, False, sl, tp, rr, original_stop_pct,
                              original_stop_pct, safe_stop_pct, 0.0,
                              "no_safe_stop_capacity")

    if original_stop_pct <= safe_stop_pct:
        return GeometryResult(True, False, sl, tp, rr, original_stop_pct,
                              original_stop_pct, safe_stop_pct, 1.0,
                              "already_leverage_compatible")

    retained = safe_stop_pct / original_stop_pct
    if retained < _MIN_RETAINED_STOP_FRACTION:
        return GeometryResult(False, False, sl, tp, rr, original_stop_pct,
                              original_stop_pct, safe_stop_pct, retained,
                              "required_compression_too_large")

    new_risk = entry * safe_stop_pct / 100.0
    if direction == "LONG":
        new_sl = entry - new_risk
        new_tp = entry + new_risk * rr
    else:
        new_sl = entry + new_risk
        new_tp = entry - new_risk * rr

    if not all(math.isfinite(v) and v > 0 for v in (new_sl, new_tp)):
        return GeometryResult(False, False, sl, tp, rr, original_stop_pct,
                              original_stop_pct, safe_stop_pct, retained,
                              "non_finite_adjusted_geometry")

    return GeometryResult(
        True, True, new_sl, new_tp, rr, original_stop_pct, safe_stop_pct,
        safe_stop_pct, retained, "compressed_for_configured_leverage"
    )


def install(Analyzer, strategy, liquidation, log) -> None:
    if getattr(Analyzer, "_leverage_safe_signal_geometry_installed", False):
        return

    original = Analyzer.analyze_mtf

    def analyze_mtf_leverage_safe(self, symbol, k15, k1h, k4h,
                                  min_score=60, fee_mult=2.0, vol_mult=1.0):
        sig = original(
            self, symbol, k15, k1h, k4h,
            min_score=min_score, fee_mult=fee_mult, vol_mult=vol_mult,
        )
        if sig is None:
            return None

        result = adapt_levels(
            liquidation,
            symbol=sig.symbol,
            direction=sig.direction,
            entry=float(sig.entry),
            sl=float(sig.sl),
            tp=float(sig.tp),
            leverage=int(strategy.cfg.LEVERAGE),
        )
        if not result.allowed:
            log.warning(
                "[LEVERAGE_SAFE_GEOMETRY] symbol=%s direction=%s result=HOLD "
                "reason=%s leverage=%sx original_stop=%.3f%% safe_stop=%.3f%% "
                "retained=%.1f%% execution_effect=NONE",
                sig.symbol, sig.direction, result.reason, strategy.cfg.LEVERAGE,
                result.original_stop_pct, result.safe_stop_pct,
                result.retained_fraction * 100.0,
            )
            return None
        if not result.adjusted:
            return sig

        # Exchange tick quantization and the final liquidation guard still run
        # later. Here we keep sufficient decimal precision for those layers.
        new_sl = round(result.sl, 8)
        new_tp = round(result.tp, 8)
        move_to_tp_pct = abs(new_tp - float(sig.entry)) / float(sig.entry) * 100.0
        total_fees_pct = float(getattr(sig, "total_fees", 0.0) or 0.0)
        if move_to_tp_pct < total_fees_pct * float(fee_mult):
            log.warning(
                "[LEVERAGE_SAFE_GEOMETRY] symbol=%s result=HOLD "
                "reason=post_compression_fee_viability move_to_tp=%.3f%% "
                "required=%.3f%% execution_effect=NONE",
                sig.symbol, move_to_tp_pct, total_fees_pct * float(fee_mult),
            )
            return None

        sig.sl = new_sl
        sig.tp = new_tp
        # Current strategy does not provide independent partial targets; keep
        # all target aliases coherent with the exact trade NEXUS will review.
        sig.tp1 = new_tp
        sig.tp2 = new_tp
        sig.rr = round(result.rr, 2)
        sig.rr1 = sig.rr
        sig.rr2 = sig.rr
        sig.expected_pnl = round(move_to_tp_pct - total_fees_pct, 3)
        sig.reason = (
            f"{getattr(sig, 'reason', '')} | LEV_SAFE:{strategy.cfg.LEVERAGE}x "
            f"SL{result.original_stop_pct:.2f}%→{result.final_stop_pct:.2f}%"
        ).strip(" |")

        log.warning(
            "[LEVERAGE_SAFE_GEOMETRY] symbol=%s direction=%s result=ADJUSTED "
            "leverage=%sx stop=%.3f%%->%.3f%% rr=%.3f retained=%.1f%% "
            "extra_liq_headroom=%.2fpp downstream_NEXUS_and_risk_required=true",
            sig.symbol, sig.direction, strategy.cfg.LEVERAGE,
            result.original_stop_pct, result.final_stop_pct, result.rr,
            result.retained_fraction * 100.0, _EXTRA_LIQ_HEADROOM_PCT,
        )
        return sig

    Analyzer.analyze_mtf = analyze_mtf_leverage_safe
    Analyzer._leverage_safe_signal_geometry_installed = True
    log.warning(
        "[LEVERAGE_SAFE_GEOMETRY] installed leverage=%sx extra_liq_headroom=%.2fpp "
        "min_retained_stop=%.0f%% leverage_unchanged=true liquidation_guard_unchanged=true",
        strategy.cfg.LEVERAGE, _EXTRA_LIQ_HEADROOM_PCT,
        _MIN_RETAINED_STOP_FRACTION * 100.0,
    )
