"""Final LIVE pilot sizing authority: risk-authoritative, minimum of all caps.

History: this wrapper used to make "50% of available collateral as initial
margin at configured leverage" the quantity authority (``OPERATOR_50PCT_EQUITY``)
and downgraded RiskManagerV3 to a non-authoritative validation gate. At 50x
that sized positions whose stop-out cost ~25% of equity.

Now the final LIVE quantity is::

    final_qty = floor_lot(min(risk_manager_qty, canonical_qty))

where ``risk_manager_qty`` is the executable-core RiskManagerV3 adapter result
and ``canonical_qty`` is an independent recomputation by
``risk_policy.size_new_entry`` from the confirmed capital snapshot and the
fresh pilot available balance. Both are equity stop-risk authoritative; the
operator 50% margin figure is only one of several CAPS. The result is then
proven against the equity-based ``final_loss_budget`` before it can reach the
dispatch path. Any missing, unconfirmed, non-finite or inconsistent input
fails closed (quantity 0, entry blocked). The quantity is never escalated to
an exchange minimum, and the technical stop is never moved.
"""
from __future__ import annotations

import math

from bot.config import cfg
from bot import risk_policy

# Kept for backwards-compatible imports; it is a CAP, not a target.
MARGIN_FRACTION = risk_policy.DEFAULT_OPERATOR_MARGIN_CAP_PCT


def _select_final_quantity(*, target_qty: float, risk_qty: float) -> float:
    """Return the smaller of two positive finite quantities, otherwise 0."""
    values = (float(target_qty), float(risk_qty))
    if any(not math.isfinite(v) or v <= 0 for v in values):
        return 0.0
    return min(values)


def _effective_risk_pct(engine) -> float:
    getter = getattr(engine, "_effective_risk_pct", None)
    value = float(getter()) if callable(getter) else float(cfg.MAX_RISK_PCT)
    return min(value, float(cfg.MAX_RISK_PCT))


def size_pilot_entry(engine, symbol: str, info: dict, price: float, signal, log):
    """Return ``(final_qty, detail)``; ``final_qty == 0`` means BLOCK."""
    price_f = float(price)
    direction = str(getattr(signal, "direction", "")).upper()
    stop = float(getattr(signal, "sl", float("nan")))

    snapshot = getattr(getattr(engine, "risk", None), "professional_snapshot", None)
    capital = getattr(snapshot, "capital", None)
    if snapshot is None or capital is None or getattr(snapshot, "confirmed", False) is not True:
        return 0.0, "capital_snapshot_unconfirmed"

    pilot_available = float(getattr(engine, "_pilot_available_balance", 0.0) or 0.0)
    available = min(float(capital.available_collateral), pilot_available)
    equity = float(capital.equity)

    policy = risk_policy.load_policy(cfg)
    from bot.professional_risk_adapter import (
        conservative_cost_fraction, max_adverse_entry_drift, _maintenance_margin_rate,
    )
    cost = conservative_cost_fraction(symbol, policy)
    instruments = getattr(engine, "instruments", {}) or {}
    open_risks = [
        risk_policy.projected_open_risk(p, instruments.get(sym), cost)
        for sym, p in (getattr(engine, "positions", {}) or {}).items()
    ]
    risk_pct = _effective_risk_pct(engine)
    rules = risk_policy.QuantityRules.from_instrument(info)
    canonical = risk_policy.size_new_entry(
        policy=policy, equity=equity, available=available, entry=price_f, stop=stop,
        direction=direction, rules=rules, cost_fraction=cost, risk_pct=risk_pct,
        maintenance_margin_rate=_maintenance_margin_rate(info), open_risks=open_risks,
        max_adverse_entry_drift=max_adverse_entry_drift(),
    )
    if not canonical.allowed:
        log.warning("[FINAL_SIZING_INVARIANT] symbol=%s canonical %s", symbol, canonical.log_fields())
        return 0.0, f"canonical_{canonical.reason}"

    risk_qty = float(engine.risk.size(
        symbol, price_f, engine.instruments, open_positions=engine.positions,
    ))
    combined = _select_final_quantity(target_qty=canonical.qty, risk_qty=risk_qty)
    if combined <= 0:
        return 0.0, "risk_manager_quantity_invalid_or_zero"
    final_d = rules.floor_base(combined)
    if final_d <= 0 or final_d < rules.minimum_base(price_f):
        return 0.0, "exchange_minimum_exceeds_safe_quantity"
    final_qty = float(final_d)

    from bot.final_loss_budget import validate
    validate(final_qty, price_f, stop, direction, float(cfg.LEVERAGE), cost,
             equity=equity, risk_pct=risk_pct)
    margin = final_qty * price_f / float(cfg.LEVERAGE)
    cap = available * min(policy.max_margin_pct, policy.operator_margin_cap_pct)
    if not math.isfinite(margin) or margin > cap * (1 + 1e-9):
        return 0.0, "margin_cap_exceeded"
    return final_qty, {
        "risk_qty": risk_qty, "canonical": canonical, "cost": cost,
        "equity": equity, "risk_pct": risk_pct, "margin": margin,
    }


def install(engine_module, pilot_cap, log) -> None:
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

        signal = pilot_cap._PILOT_SIGNAL.get()
        setup_id = str(getattr(signal, "_bgx_setup_id", "") or "UNKNOWN")
        try:
            final_qty, detail = size_pilot_entry(engine, symbol, info, price, signal, log)
        except Exception as exc:
            # Risk/sizing ambiguity is fail-closed. The failure type is logged;
            # nothing is swallowed silently.
            from bot.final_loss_budget import reason_from_exception
            log.critical(
                "[FINAL_SIZING_INVARIANT] symbol=%s setup_id=%s result=BLOCK reason=%s error=%s",
                symbol, setup_id, reason_from_exception(exc), type(exc).__name__,
            )
            pilot_cap._PILOT_FINAL_QTY.set(0.0)
            return 0.0

        if final_qty <= 0:
            log.critical(
                "[FINAL_SIZING_INVARIANT] symbol=%s setup_id=%s result=BLOCK reason=%s",
                symbol, setup_id, detail,
            )
            pilot_cap._PILOT_FINAL_QTY.set(0.0)
            return 0.0

        from bot.final_loss_budget import emit_telemetry
        emit_telemetry(
            log, symbol=symbol, setup_id=setup_id, stage="FINAL_SIZING_INVARIANT",
            qty=final_qty, entry=float(price), stop=signal.sl, direction=signal.direction,
            leverage=float(cfg.LEVERAGE), cost_fraction=detail["cost"], result="PASS",
            specific_reason="within_equity_risk_budget", equity=detail["equity"],
            risk_pct=detail["risk_pct"], risk_v3_qty=detail["risk_qty"],
        )
        pilot_cap._PILOT_FINAL_QTY.set(final_qty)
        log.warning(
            "[FINAL_SIZING_INVARIANT] symbol=%s setup_id=%s result=PASS final_qty=%.12g "
            "risk_manager_qty=%.12g margin=%.6f authority=RISK_POLICY_MIN_OF_CAPS %s",
            symbol, setup_id, final_qty, detail["risk_qty"], detail["margin"],
            detail["canonical"].log_fields(),
        )
        return final_qty

    engine_module.minimum_base_quantity = _final_risk_authoritative_quantity
    engine_module._final_sizing_invariants_installed = True
    log.critical(
        "[FINAL_SIZING_INVARIANT] installed=true sizing_authority=RISK_POLICY_MIN_OF_CAPS "
        "caps=risk,margin,operator_margin_cap,liquidation,portfolio,exchange_lot "
        "operator_margin=CAP_ONLY loss_budget=EQUITY_RISK_PCT fail_closed=true"
    )
