"""Operator-requested LIVE sizing and drawdown semantics.

This policy intentionally changes two execution semantics for the controlled
LIVE pilot while leaving leverage, entry thresholds, AI approval, integrity,
ownership, protection and duplicate-order guards untouched:

* target initial margin = 50% of freshly authenticated available collateral;
* account drawdown remains calculated/persisted/logged but is advisory only and
  no longer vetoes a new entry by itself.

The margin target is applied after the existing pilot sizing wrappers have been
installed so the old 50%-of-available *position-notional* target and the
stop-risk quantity cap cannot silently shrink the operator's requested margin
allocation. Stop geometry is still calculated and logged by the risk stack, but
it is telemetry rather than the final quantity cap in this policy.
"""
from __future__ import annotations

import math

from bot.config import cfg


MARGIN_FRACTION = 0.50


def _install_drawdown_advisory(TradingEngine_or_log, log=None) -> None:
    """Install advisory drawdown semantics.

    Backward compatible with the previous private helper signature
    ``_install_drawdown_advisory(log)`` used by regression tests. The engine
    wrapper is installed only when a TradingEngine class is explicitly passed.
    """
    if log is None:
        TradingEngine = None
        log = TradingEngine_or_log
    else:
        TradingEngine = TradingEngine_or_log

    from bot.risk import RiskManager
    from bot.risk_manager_v3 import RiskManagerV3

    if not getattr(RiskManager, "_operator_drawdown_advisory", False):
        def _legacy_can_open(self, n: int) -> bool:
            if not self._ready:
                log.warning(
                    "⛔ RiskManager não inicializado (saldo lido: $%.2f) — scan bloqueado",
                    float(self.balance),
                )
                return False
            if not self.balance_confirmed or self.balance <= 0:
                log.warning("[BALANCE] new entries blocked: zero or unconfirmed balance")
                return False
            if self.drawdown >= cfg.MAX_DRAWDOWN:
                log.warning(
                    "[DRAWDOWN_ADVISORY] drawdown=%.2f%% configured_limit=%.2f%% "
                    "entries_blocked=false execution_effect=NONE",
                    float(self.drawdown) * 100.0,
                    float(cfg.MAX_DRAWDOWN) * 100.0,
                )
            if n >= cfg.MAX_POSITIONS:
                log.info("⛔ %s/%s posições → aguardando", n, cfg.MAX_POSITIONS)
                return False
            return True

        RiskManager.can_open = _legacy_can_open
        RiskManager._operator_drawdown_advisory = True

    if not getattr(RiskManagerV3, "_operator_drawdown_advisory", False):
        def _v3_can_open(self, open_positions: int) -> bool:
            if not self.confirmed:
                return False
            if self.equity <= 0 or self.available_collateral <= 0:
                return False
            if self.drawdown >= cfg.MAX_DRAWDOWN:
                log.warning(
                    "[DRAWDOWN_ADVISORY_V3] drawdown=%.2f%% configured_limit=%.2f%% "
                    "entries_blocked=false execution_effect=NONE",
                    float(self.drawdown) * 100.0,
                    float(cfg.MAX_DRAWDOWN) * 100.0,
                )
            if open_positions >= cfg.MAX_POSITIONS:
                return False
            return True

        RiskManagerV3.can_open = _v3_can_open
        RiskManagerV3._operator_drawdown_advisory = True

    # engine.py still contains a legacy side effect inside _update_balance():
    # crossing MAX_DRAWDOWN sets self.active=False after RiskManager.update().
    # Keep the accounting + one-shot alert intact, but neutralize only that
    # drawdown-originated deactivation. A pre-existing inactive state is never
    # re-enabled here, so unrelated operational/safety pauses remain authoritative.
    if TradingEngine is not None and not getattr(
        TradingEngine, "_operator_drawdown_engine_advisory", False
    ):
        previous_update_balance = TradingEngine._update_balance

        async def _update_balance_advisory(self, *args, **kwargs):
            was_active = bool(getattr(self, "active", False))
            result = await previous_update_balance(self, *args, **kwargs)

            risk = getattr(self, "risk", None)
            drawdown = float(getattr(risk, "drawdown", 0.0) or 0.0)
            dd_alerted = bool(getattr(self, "_dd_alerted", False))
            became_inactive = was_active and not bool(getattr(self, "active", False))

            if became_inactive and dd_alerted and drawdown >= float(cfg.MAX_DRAWDOWN):
                self.active = True
                log.warning(
                    "[DRAWDOWN_ADVISORY_ENGINE] drawdown=%.2f%% configured_limit=%.2f%% "
                    "legacy_pause_neutralized=true active_restored=true execution_effect=NONE",
                    drawdown * 100.0,
                    float(cfg.MAX_DRAWDOWN) * 100.0,
                )
            return result

        TradingEngine._update_balance = _update_balance_advisory
        TradingEngine._operator_drawdown_engine_advisory = True


def _install_margin_sizing(log) -> None:
    """Make 50% of fresh available collateral the LIVE pilot margin target."""
    from bot import engine as engine_module
    from bot import pilot_live_runtime
    from bot import pilot_risk_cap_hardening as pilot_cap

    if getattr(engine_module, "_operator_margin_sizing_installed", False):
        return

    previous_minimum = engine_module.minimum_base_quantity

    def _margin_target_quantity(info, price):
        engine = pilot_cap._PILOT_ENGINE.get()
        symbol = pilot_cap._PILOT_SYMBOL.get()
        if engine is None or not symbol:
            return previous_minimum(info, price)
        if getattr(engine, "paper_trade", False) or not bool(
            getattr(getattr(engine, "pilot", None), "enabled", False)
        ):
            return previous_minimum(info, price)

        available = float(getattr(engine, "_pilot_available_balance", 0.0) or 0.0)
        leverage = float(cfg.LEVERAGE)
        price_f = float(price)
        if (
            not math.isfinite(available) or available <= 0
            or not math.isfinite(leverage) or leverage <= 0
            or not math.isfinite(price_f) or price_f <= 0
        ):
            log.critical(
                "[PILOT_MARGIN_SIZING] symbol=%s result=BLOCK reason=invalid_context "
                "available=%s leverage=%s price=%s",
                symbol, available, leverage, price_f,
            )
            pilot_cap._PILOT_FINAL_QTY.set(0.0)
            return 0.0

        target_margin = available * MARGIN_FRACTION
        target_notional = target_margin * leverage
        try:
            target_qty = float(
                pilot_live_runtime._pilot_quantity_for_notional(
                    info, price_f, target_notional
                )
            )
        except Exception as exc:
            log.critical(
                "[PILOT_MARGIN_SIZING] symbol=%s result=BLOCK reason=quantity_%s",
                symbol, type(exc).__name__,
            )
            pilot_cap._PILOT_FINAL_QTY.set(0.0)
            return 0.0

        # Keep the professional stop-risk computation visible for diagnostics,
        # but do not let it silently redefine the operator-requested margin
        # allocation. Other hard execution/risk gates remain downstream.
        risk_qty = 0.0
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
            log.warning(
                "[PILOT_MARGIN_SIZING] symbol=%s stop_risk_telemetry=%s",
                symbol, type(exc).__name__,
            )

        if not math.isfinite(target_qty) or target_qty <= 0:
            pilot_cap._PILOT_FINAL_QTY.set(0.0)
            return 0.0

        actual_notional = target_qty * price_f
        actual_margin = actual_notional / leverage
        pilot_cap._PILOT_FINAL_QTY.set(target_qty)
        log.warning(
            "[PILOT_MARGIN_SIZING] symbol=%s result=PASS available=%.6f "
            "margin_pct=%.2f%% target_margin=%.6f leverage=%.0fx "
            "target_notional=%.6f qty=%.12g actual_margin=%.6f "
            "stop_risk_qty_advisory=%.12g authority=operator_margin_policy",
            symbol,
            available,
            MARGIN_FRACTION * 100.0,
            target_margin,
            leverage,
            target_notional,
            target_qty,
            actual_margin,
            risk_qty,
        )
        return target_qty

    engine_module.minimum_base_quantity = _margin_target_quantity
    engine_module._operator_margin_sizing_installed = True


def install(TradingEngine, log) -> None:
    """Install after all controlled-pilot sizing/risk wrappers."""
    _install_drawdown_advisory(TradingEngine, log)
    _install_margin_sizing(log)
    log.critical(
        "[OPERATOR_RUNTIME_POLICY] installed margin_target=50pct_available "
        "leverage=%sx drawdown=advisory_only railway_variables_unchanged=true",
        cfg.LEVERAGE,
    )
