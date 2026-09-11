"""Risk-authoritative sizing guard for the controlled LIVE pilot.

The operator target remains 50% of authenticated available USDT as POSITION
NOTIONAL, as implemented by ``pilot_live_runtime``. This guard makes that target
a cap/target rather than an override of stop-risk sizing:

    final_qty = min(stop_risk_qty, pilot_target_qty)

The stop-risk quantity is produced by the existing ProfessionalRiskAdapter /
RiskManagerV3 after the NEXUS decision has prepared validated entry/stop geometry.
No downstream layer may increase quantity above that risk-authoritative value.

This module does not authorize LIVE mode, change leverage, modify Railway
variables, weaken PilotGuard, or submit orders by itself.
"""
from __future__ import annotations

import contextvars
import math


_PILOT_ENGINE = contextvars.ContextVar("nexus_pilot_risk_engine", default=None)
_PILOT_SYMBOL = contextvars.ContextVar("nexus_pilot_risk_symbol", default=None)


def _select_final_quantity(*, target_qty: float, risk_qty: float) -> float:
    """Return the smaller positive finite quantity, otherwise fail closed."""
    values = (float(target_qty), float(risk_qty))
    if any((not math.isfinite(v) or v <= 0) for v in values):
        return 0.0
    return min(values)


def install(TradingEngine, log) -> None:
    """Install after ``pilot_live_runtime`` so its 50% target remains intact."""
    if getattr(TradingEngine, "_pilot_risk_cap_hardening_installed", False):
        return

    from bot import engine as engine_module

    original_open = TradingEngine._open
    original_minimum = engine_module.minimum_base_quantity

    async def _open_with_pilot_risk_context(self, sig, *args, **kwargs):
        # PAPER and non-pilot execution keep their existing behavior.
        if getattr(self, "paper_trade", False) or not bool(
            getattr(getattr(self, "pilot", None), "enabled", False)
        ):
            return await original_open(self, sig, *args, **kwargs)

        token_engine = _PILOT_ENGINE.set(self)
        token_symbol = _PILOT_SYMBOL.set(getattr(sig, "symbol", None))
        try:
            return await original_open(self, sig, *args, **kwargs)
        finally:
            _PILOT_SYMBOL.reset(token_symbol)
            _PILOT_ENGINE.reset(token_engine)

    def _risk_authoritative_pilot_quantity(info, price):
        engine = _PILOT_ENGINE.get()
        symbol = _PILOT_SYMBOL.get()
        if engine is None or not symbol:
            return original_minimum(info, price)

        # ``original_minimum`` is the pilot-aware hook installed immediately
        # before this guard. In the controlled pilot it yields the quantity for
        # the operator's 50%-of-available position-notional target.
        target_qty = float(original_minimum(info, price))

        try:
            risk_qty = float(
                engine.risk.size(
                    symbol,
                    float(price),
                    engine.instruments,
                    open_positions=engine.positions,
                )
            )
        except Exception as exc:
            log.critical(
                "[PILOT_RISK_CAP] symbol=%s result=BLOCK reason=risk_sizing_%s",
                symbol,
                type(exc).__name__,
            )
            return 0.0

        final_qty = _select_final_quantity(
            target_qty=target_qty,
            risk_qty=risk_qty,
        )
        if final_qty <= 0:
            log.critical(
                "[PILOT_RISK_CAP] symbol=%s result=BLOCK target_qty=%.12g "
                "risk_qty=%.12g reason=nonpositive_or_invalid",
                symbol,
                target_qty,
                risk_qty,
            )
            return 0.0

        log.warning(
            "[PILOT_RISK_CAP] symbol=%s target_qty=%.12g risk_qty=%.12g "
            "final_qty=%.12g authority=RiskManagerV3 target_policy=50pct_available_notional",
            symbol,
            target_qty,
            risk_qty,
            final_qty,
        )
        return final_qty

    TradingEngine._open = _open_with_pilot_risk_context
    engine_module.minimum_base_quantity = _risk_authoritative_pilot_quantity
    TradingEngine._pilot_risk_cap_hardening_installed = True

    log.critical(
        "[PILOT_RISK_CAP] installed: 50pct available balance remains the position-"
        "notional target; RiskManagerV3 is the maximum quantity authority"
    )
