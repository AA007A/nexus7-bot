"""Final LIVE pilot sizing authority.

This wrapper is intentionally installed after the operator 50%-margin policy.
The operator policy computes the requested target using 50% of freshly
authenticated available collateral and the configured leverage. This module
then makes RiskManagerV3 an inescapable maximum-quantity authority:

    final_qty = min(operator_50pct_margin_target_qty, stop_risk_qty)

The 50% target and configured leverage are not changed. Invalid/missing risk
sizing, non-finite quantities, or a final quantity whose implied initial margin
exceeds the 50% target fail closed before dispatch.
"""
from __future__ import annotations

import math

from bot.config import cfg


MARGIN_FRACTION = 0.50


def _select_final_quantity(*, target_qty: float, risk_qty: float) -> float:
    values = (float(target_qty), float(risk_qty))
    if any(not math.isfinite(v) or v <= 0 for v in values):
        return 0.0
    return min(values)


def install(engine_module, pilot_cap, log) -> None:
    """Install the final sizing wrapper after operator_runtime_policy."""
    if getattr(engine_module, "_final_sizing_invariants_installed", False):
        return

    previous_minimum = engine_module.minimum_base_quantity

    def _final_risk_authoritative_quantity(info, price):
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
                symbol,
                type(exc).__name__,
            )
            pilot_cap._PILOT_FINAL_QTY.set(0.0)
            return 0.0

        if any(
            not math.isfinite(v) or v <= 0
            for v in (target_qty, price_f, available, leverage)
        ):
            log.critical(
                "[FINAL_SIZING_INVARIANT] symbol=%s result=BLOCK reason=invalid_context "
                "target_qty=%s price=%s available=%s leverage=%s",
                symbol,
                target_qty,
                price_f,
                available,
                leverage,
            )
            pilot_cap._PILOT_FINAL_QTY.set(0.0)
            return 0.0

        try:
            risk_qty = float(
                engine.risk.size(
                    symbol,
                    price_f,
                    engine.instruments,
                    open_positions=engine.positions,
                )
            )
        except Exception as exc:
            log.critical(
                "[FINAL_SIZING_INVARIANT] symbol=%s result=BLOCK reason=risk_sizing_%s",
                symbol,
                type(exc).__name__,
            )
            pilot_cap._PILOT_FINAL_QTY.set(0.0)
            return 0.0

        final_qty = _select_final_quantity(
            target_qty=target_qty,
            risk_qty=risk_qty,
        )
        if final_qty <= 0:
            log.critical(
                "[FINAL_SIZING_INVARIANT] symbol=%s result=BLOCK reason=invalid_quantity "
                "target_qty=%.12g risk_qty=%.12g",
                symbol,
                target_qty,
                risk_qty,
            )
            pilot_cap._PILOT_FINAL_QTY.set(0.0)
            return 0.0

        target_margin = available * MARGIN_FRACTION
        final_notional = final_qty * price_f
        final_margin = final_notional / leverage
        tolerance = max(1e-9, target_margin * 1e-6)
        if not math.isfinite(final_margin) or final_margin > target_margin + tolerance:
            log.critical(
                "[FINAL_SIZING_INVARIANT] symbol=%s result=BLOCK reason=margin_cap_exceeded "
                "final_margin=%.12g target_margin=%.12g margin_pct=%.2f%% leverage=%.0fx",
                symbol,
                final_margin,
                target_margin,
                MARGIN_FRACTION * 100.0,
                leverage,
            )
            pilot_cap._PILOT_FINAL_QTY.set(0.0)
            return 0.0

        pilot_cap._PILOT_FINAL_QTY.set(final_qty)
        log.warning(
            "[FINAL_SIZING_INVARIANT] symbol=%s result=PASS target_qty=%.12g "
            "risk_qty=%.12g final_qty=%.12g target_margin=%.6f final_margin=%.6f "
            "margin_pct=%.2f%% leverage=%.0fx authority=RiskManagerV3_plus_operator_margin_cap",
            symbol,
            target_qty,
            risk_qty,
            final_qty,
            target_margin,
            final_margin,
            MARGIN_FRACTION * 100.0,
            leverage,
        )
        return final_qty

    engine_module.minimum_base_quantity = _final_risk_authoritative_quantity
    engine_module._final_sizing_invariants_installed = True
    log.critical(
        "[FINAL_SIZING_INVARIANT] installed=true operator_margin_target=50pct_available "
        "risk_authority=RiskManagerV3 configured_leverage_unchanged=true fail_closed=true"
    )
