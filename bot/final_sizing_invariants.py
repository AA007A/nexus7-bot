"""Final LIVE pilot sizing authority.

Operator policy owns quantity: 50% of freshly authenticated available
collateral is used as initial margin at configured leverage. RiskManagerV3 is
still mandatory as a fail-closed validation gate, but does not silently shrink
an otherwise valid operator target.
"""
from __future__ import annotations

import math

from bot.config import cfg

MARGIN_FRACTION = 0.50


def _select_final_quantity(*, target_qty: float, risk_qty: float) -> float:
    """Return operator target when both target and risk validation are valid."""
    values = (float(target_qty), float(risk_qty))
    if any(not math.isfinite(v) or v <= 0 for v in values):
        return 0.0
    return float(target_qty)


def install(engine_module, pilot_cap, log) -> None:
    if getattr(engine_module, "_final_sizing_invariants_installed", False):
        return

    previous_minimum = engine_module.minimum_base_quantity

    def _final_operator_authoritative_quantity(info, price):
        engine = pilot_cap._PILOT_ENGINE.get()
        symbol = pilot_cap._PILOT_SYMBOL.get()
        if engine is None or not symbol:
            return previous_minimum(info, price)
        if getattr(engine, "paper_trade", False) or not bool(
            getattr(getattr(engine, "pilot", None), "enabled", False)
        ):
            return previous_minimum(info, price)

        try:
            target_qty = float(previous_minimum(info, price))
            price_f = float(price)
            available = float(getattr(engine, "_pilot_available_balance", 0.0) or 0.0)
            leverage = float(cfg.LEVERAGE)
        except (TypeError, ValueError) as exc:
            log.critical(
                "[FINAL_SIZING_INVARIANT] symbol=%s result=BLOCK reason=context_%s",
                symbol, type(exc).__name__,
            )
            pilot_cap._PILOT_FINAL_QTY.set(0.0)
            return 0.0

        if any(not math.isfinite(v) or v <= 0 for v in (target_qty, price_f, available, leverage)):
            log.critical("[FINAL_SIZING_INVARIANT] symbol=%s result=BLOCK reason=invalid_context", symbol)
            pilot_cap._PILOT_FINAL_QTY.set(0.0)
            return 0.0

        try:
            risk_qty = float(engine.risk.size(
                symbol, price_f, engine.instruments, open_positions=engine.positions,
            ))
        except Exception as exc:
            log.critical(
                "[FINAL_SIZING_INVARIANT] symbol=%s result=BLOCK reason=risk_validation_%s",
                symbol, type(exc).__name__,
            )
            pilot_cap._PILOT_FINAL_QTY.set(0.0)
            return 0.0

        final_qty = _select_final_quantity(target_qty=target_qty, risk_qty=risk_qty)
        if final_qty <= 0:
            log.critical(
                "[FINAL_SIZING_INVARIANT] symbol=%s result=BLOCK reason=invalid_quantity target_qty=%.12g risk_validation_qty=%.12g",
                symbol, target_qty, risk_qty,
            )
            pilot_cap._PILOT_FINAL_QTY.set(0.0)
            return 0.0

        target_margin = available * MARGIN_FRACTION
        final_margin = (final_qty * price_f) / leverage
        tolerance = max(1e-9, target_margin * 1e-6)
        if not math.isfinite(final_margin) or final_margin > target_margin + tolerance:
            log.critical(
                "[FINAL_SIZING_INVARIANT] symbol=%s result=BLOCK reason=margin_cap_exceeded final_margin=%.12g target_margin=%.12g",
                symbol, final_margin, target_margin,
            )
            pilot_cap._PILOT_FINAL_QTY.set(0.0)
            return 0.0

        pilot_cap._PILOT_FINAL_QTY.set(final_qty)
        log.warning(
            "[FINAL_SIZING_INVARIANT] symbol=%s result=PASS target_qty=%.12g risk_validation_qty=%.12g final_qty=%.12g target_margin=%.6f final_margin=%.6f margin_pct=50.00%% leverage=%.0fx authority=OPERATOR_50PCT_EQUITY risk_manager_role=VALIDATION_GATE",
            symbol, target_qty, risk_qty, final_qty, target_margin, final_margin, leverage,
        )
        return final_qty

    engine_module.minimum_base_quantity = _final_operator_authoritative_quantity
    engine_module._final_sizing_invariants_installed = True
    log.critical(
        "[FINAL_SIZING_INVARIANT] installed=true operator_margin_target=50pct_available sizing_authority=OPERATOR_50PCT_EQUITY risk_manager_role=VALIDATION_GATE configured_leverage_unchanged=true fail_closed=true"
    )
