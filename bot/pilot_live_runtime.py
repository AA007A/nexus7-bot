"""Runtime hardening used only after explicit controlled-pilot release.

The release gate is evaluated elsewhere. This module does not itself authorize
LIVE execution. When installed, it keeps account-equity drawdown durable and
cash-flow aware, tracks free collateral separately, performs authenticated
read-only exposure and private-WS preflight, and refreshes those checks
immediately before each candidate reaches the normal _open pipeline.

For the controlled LIVE pilot, the requested entry-size policy is expressed as
POSITION NOTIONAL, not margin collateral: by default the target position value
is 50% of the currently available USDT balance. Example: available ~= 20 USDT
=> target position notional ~= 10 USDT. At 50x leverage this uses only about
0.20 USDT initial margin before fees; it does NOT mean risking 10 USDT of margin.
Exchange lot-size rounding may make the final notional slightly larger than the
target. All existing AI, RR/EV, drawdown, liquidation, affordability, exposure,
durable-state and release gates still run and may block an entry.

External/manual positions remain governed by pilot_external_position_guard and
pilot_exposure_capacity; this module never adopts, amends, reduces or closes
one.
"""
from __future__ import annotations

import contextvars
import os
import time
from decimal import Decimal, ROUND_FLOOR

from bot import account_balance_semantics as account_semantics
from bot import capital_flow_reconciliation as capital_flows
from bot.drawdown_persistence import restore_update_real_account_peak
from bot.quantity import quantity_rules


_PILOT_TARGET_NOTIONAL = contextvars.ContextVar(
    "nexus_pilot_target_notional", default=None
)
_PILOT_NOTIONAL_PCT = float(os.environ.get("PILOT_NOTIONAL_PCT", "0.50"))


def _pilot_quantity_for_notional(info: dict, price: float, target_notional: float) -> float:
    """Largest valid base quantity that does not exceed allocated notional."""
    if isinstance(target_notional, bool):
        raise ValueError("target_notional must be numeric")
    price_d = Decimal(str(price))
    target_d = Decimal(str(target_notional))
    if not price_d.is_finite() or price_d <= 0:
        raise ValueError("invalid price")
    if not target_d.is_finite() or target_d <= 0:
        raise ValueError("invalid target_notional")

    multiplier, lot, minimum, min_notional = quantity_rules(info)
    contracts = target_d / (price_d * multiplier)
    contracts = (contracts / lot).to_integral_value(rounding=ROUND_FLOOR) * lot
    if contracts < minimum or contracts * multiplier * price_d < min_notional:
        return 0.0
    return float(contracts * multiplier)


def _install_pilot_notional_sizing(log) -> None:
    """Patch only the LIVE-pilot minimum-lot hook used by core engine._open.

    The core engine deliberately calls ``minimum_base_quantity`` while pilot is
    enabled. Rather than weakening PilotGuard, replace that narrow hook with a
    context-scoped target. Outside a LIVE pilot candidate the original function
    is returned unchanged.
    """
    from bot import engine as engine_module

    if getattr(engine_module, "_pilot_notional_sizing_installed", False):
        return

    original_minimum = engine_module.minimum_base_quantity

    def _pilot_aware_minimum(info, price):
        target = _PILOT_TARGET_NOTIONAL.get()
        if target is None:
            return original_minimum(info, price)
        return _pilot_quantity_for_notional(info, price, target)

    engine_module.minimum_base_quantity = _pilot_aware_minimum
    engine_module._pilot_notional_sizing_installed = True
    log.critical(
        "[PILOT_LEGACY_TARGET] preliminary_only=true installed target_notional_pct=%.2f%% basis=available_balance "
        "meaning=position_notional_not_margin",
        _PILOT_NOTIONAL_PCT * 100.0,
    )


async def _refresh_account(engine, log, *, for_entry: bool = False) -> dict:
    state = await account_semantics.read_account_state(engine.client)
    equity = float(state["equity"])
    available = float(state["available"])

    account_semantics.update_risk_from_equity(engine.risk, equity)

    # KuCoin accountEquity includes external deposits/withdrawals/transfers.
    # Before enforcing the durable HWM, reconcile completed ledger cash flows
    # so a user moving capital cannot be misclassified as trading PnL.
    now = time.time()
    previous_equity = getattr(engine, "_pilot_prev_account_equity", None)
    last_flow_check = float(getattr(engine, "_pilot_last_capital_flow_check", 0.0) or 0.0)
    material_change = (
        previous_equity is None
        or abs(equity - float(previous_equity)) >= max(0.02, abs(float(previous_equity)) * 0.01)
    )
    if material_change or (now - last_flow_check) >= 300.0:
        await capital_flows.reconcile_external_capital_flows(
            engine.client,
            engine.risk,
            equity,
            strict=True,
        )
        engine._pilot_last_capital_flow_check = now

    engine._pilot_prev_account_equity = equity
    await restore_update_real_account_peak(engine.risk, equity, strict=True)

    engine._pilot_account_equity = equity
    engine._pilot_available_balance = available
    engine._pilot_balance_source = state.get("available_source", "unknown")

    # Pilot exposure-capacity evaluation consumes this normalized, timestamped
    # snapshot. Keep it synchronized with the same read used for sizing.
    snapshot = dict(state)
    snapshot["_observed_at"] = time.time()
    engine.client._last_account_overview_snapshot = snapshot

    legacy = getattr(engine.risk, "_legacy", engine.risk)
    log.info(
        "[PILOT_LIVE_BALANCE] equity=%.4f available=%.4f peak_equity=%.4f "
        "drawdown=%.2f%% collateral_basis=%s entry_refresh=%s",
        equity,
        available,
        float(getattr(legacy, "peak_balance", 0.0) or 0.0),
        float(getattr(legacy, "drawdown", 0.0) or 0.0) * 100.0,
        state.get("available_source", "unknown"),
        str(bool(for_entry)).lower(),
    )
    return state


async def _run_readonly_preflight(engine, log, *, probe_private_ws: bool) -> bool:
    from bot import private_ws_readonly_observability as prelive

    if probe_private_ws:
        ready = bool(await prelive.run(engine.client, engine.instruments, log))
    else:
        exposure_clear = bool(await prelive.refresh_account_exposure(engine.client, log))
        private_ws_ok = bool(getattr(engine.client, "_prelive_private_ws_probe_ok", False))
        ready = bool(exposure_clear and private_ws_ok)

    engine._pilot_live_prelive_ready = ready
    log.warning(
        "[PILOT_LIVE_PREFLIGHT] result=%s exposure_verified=%s exposure_clear=%s "
        "private_ws=%s",
        "PASS" if ready else "BLOCKED",
        getattr(engine.client, "_prelive_account_exposure_verified", False),
        getattr(engine.client, "_prelive_account_exposure_clear", False),
        getattr(engine.client, "_prelive_private_ws_probe_ok", False),
    )
    return ready


def install(TradingEngine, log) -> None:
    """Install LIVE-pilot-only wrappers. Caller must enforce release auth."""
    if getattr(TradingEngine, "_pilot_live_runtime_patched", False):
        return

    if not (0.0 < _PILOT_NOTIONAL_PCT <= 1.0):
        raise RuntimeError("PILOT_NOTIONAL_PCT must be >0 and <=1")

    _install_pilot_notional_sizing(log)

    original_connect = TradingEngine._connect
    original_update_balance = TradingEngine._update_balance
    original_refresh_entry_balance = TradingEngine._refresh_entry_balance
    original_open = TradingEngine._open

    async def _connect_live_pilot(self, *args, **kwargs):
        if getattr(self, "paper_trade", False):
            return await original_connect(self, *args, **kwargs)

        result = await original_connect(self, *args, **kwargs)
        if not getattr(self, "connected", False):
            self._pilot_live_prelive_ready = False
            return result

        try:
            await _refresh_account(self, log)
            await _run_readonly_preflight(self, log, probe_private_ws=True)
        except Exception as exc:
            self._pilot_live_prelive_ready = False
            self.risk.balance_confirmed = False
            log.critical(
                "[PILOT_LIVE_PREFLIGHT] result=BLOCKED reason=%s action=no_new_entry",
                type(exc).__name__,
            )
        return result

    async def _update_balance_live_pilot(self, *args, **kwargs):
        if getattr(self, "paper_trade", False):
            return await original_update_balance(self, *args, **kwargs)
        try:
            state = await _refresh_account(self, log)
            self.risk.balance_confirmed = True
            return state
        except Exception as exc:
            self.risk.balance_confirmed = False
            log.critical(
                "[PILOT_LIVE_BALANCE] result=BLOCKED reason=%s action=no_new_entry",
                type(exc).__name__,
            )
            return None

    async def _refresh_entry_balance_live_pilot(self, *args, **kwargs):
        if getattr(self, "paper_trade", False):
            return await original_refresh_entry_balance(self, *args, **kwargs)
        try:
            state = await _refresh_account(self, log, for_entry=True)
            available = float(state["available"])
            self.risk.balance_confirmed = True

            # The legacy _open implementation uses risk.balance for its two
            # immediate affordability checks. During that narrow call only,
            # expose free collateral there; durable peak/drawdown was already
            # updated from accountEquity above and is restored after _open.
            if getattr(self, "_pilot_open_in_progress", False):
                self.risk.balance = available
            if available <= 0:
                log.warning("[PILOT_LIVE_BALANCE] entry blocked: available collateral <= 0")
                return False
            return True
        except Exception as exc:
            self.risk.balance_confirmed = False
            log.critical(
                "[PILOT_LIVE_BALANCE] entry blocked reason=%s",
                type(exc).__name__,
            )
            return False

    async def _open_live_pilot(self, sig, *args, **kwargs):
        if getattr(self, "paper_trade", False):
            return await original_open(self, sig, *args, **kwargs)

        # Re-read account exposure immediately before entering the normal
        # AI/risk/PilotGuard pipeline. This is read-only and fails closed.
        try:
            if not await _run_readonly_preflight(self, log, probe_private_ws=False):
                log.warning(
                    "[PILOT_LIVE_GATE] symbol=%s stage=PRELIVE result=BLOCK",
                    getattr(sig, "symbol", "?"),
                )
                return None
            await self.integrity.assess(self.client, self)
            if not self.integrity.can_open_new():
                log.warning(
                    "[PILOT_LIVE_GATE] symbol=%s stage=INTEGRITY result=BLOCK reason=%s",
                    getattr(sig, "symbol", "?"),
                    self.integrity.block_reason(),
                )
                return None

            # Fresh authenticated balance for the position-notional target.
            # This is intentionally not inferred from stale dashboard state.
            sizing_state = await _refresh_account(self, log, for_entry=True)
            available = float(sizing_state["available"])
            if available <= 0:
                log.warning(
                    "[PILOT_SIZING] symbol=%s result=BLOCK reason=available_balance_nonpositive",
                    getattr(sig, "symbol", "?"),
                )
                return None
            target_notional = available * _PILOT_NOTIONAL_PCT
            log.warning(
                "[PILOT_LEGACY_TARGET] preliminary_only=true final_authority=PILOT_MARGIN_SIZING symbol=%s available=%.4f pct=%.2f%% "
                "target_notional=%.4f leverage=%sx target_margin_approx=%.4f",
                getattr(sig, "symbol", "?"),
                available,
                _PILOT_NOTIONAL_PCT * 100.0,
                target_notional,
                int(getattr(__import__("bot.config", fromlist=["cfg"]).cfg, "LEVERAGE", 1)),
                target_notional / max(1, int(getattr(__import__("bot.config", fromlist=["cfg"]).cfg, "LEVERAGE", 1))),
            )
        except Exception as exc:
            log.critical(
                "[PILOT_LIVE_GATE] symbol=%s stage=PRELIVE result=BLOCK reason=%s",
                getattr(sig, "symbol", "?"),
                type(exc).__name__,
            )
            return None

        self._pilot_open_in_progress = True
        token = _PILOT_TARGET_NOTIONAL.set(target_notional)
        try:
            return await original_open(self, sig, *args, **kwargs)
        finally:
            _PILOT_TARGET_NOTIONAL.reset(token)
            self._pilot_open_in_progress = False
            # Restore risk.balance to the account-equity basis even if the
            # candidate is rejected or dispatch raises. Failure keeps future
            # entries fail-closed through balance_confirmed.
            try:
                await _refresh_account(self, log)
                self.risk.balance_confirmed = True
            except Exception as exc:
                self.risk.balance_confirmed = False
                log.critical(
                    "[PILOT_LIVE_BALANCE] post-candidate restore failed reason=%s",
                    type(exc).__name__,
                )

    TradingEngine._connect = _connect_live_pilot
    TradingEngine._update_balance = _update_balance_live_pilot
    TradingEngine._refresh_entry_balance = _refresh_entry_balance_live_pilot
    TradingEngine._open = _open_live_pilot
    TradingEngine._pilot_live_runtime_patched = True

    log.critical(
        "[PILOT_LIVE_RUNTIME] installed: cash-flow-aware durable equity drawdown + "
        "50pct-available position-notional sizing + read-only exposure/private-WS "
        "preflight; external positions immutable"
    )
