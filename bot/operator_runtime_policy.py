"""Operator LIVE runtime policy: drawdown hard gate for new exposure.

History: this module previously (a) made "50% of available collateral as
initial margin" the LIVE quantity authority and (b) let
``LIVE_RISK_OVERRIDE_APPROVED=true`` neutralize the drawdown hard gate
(runtime evidence: drawdown=59.40% vs limit=50%, ``ALLOW_NEW_ENTRIES``).

Both capabilities are removed:

* ``drawdown >= MAX_DRAWDOWN`` blocks every NEW position. The decision comes
  from ``bot.risk_policy.drawdown_entry_decision`` and cannot be changed by any
  environment variable, ``DRAWDOWN_MODE`` or override.
* ``LIVE_RISK_OVERRIDE_APPROVED`` is still read, but only so telemetry is
  truthful (``override_effect=NONE``). Per ``risk_policy.override_may_authorize``
  an override may only ever authorize risk-reducing actions (close, reduce,
  cancel exposure-increasing orders, protection repair, reconciliation), and
  none of those paths pass through this gate.
* The 50% margin figure survives only as a CAP inside the canonical sizer
  (``risk_policy.size_new_entry``). It is never a utilization target.

Existing positions keep being managed, protected and reconciled while new
entries are blocked; this gate only governs NEW exposure.

The class-method ownership recorded by ``runtime_contract_guard`` is unchanged:
this module still owns ``RiskManager.can_open``, ``RiskManagerV3.can_open``,
``TradingEngine._update_balance`` and ``TradingEngine.run``.
"""
from __future__ import annotations

import os

from bot.config import cfg
from bot import risk_policy
from bot import startup_ready_notification


# Retained as the name of the operator margin CAP (not a target).
MARGIN_FRACTION = risk_policy.DEFAULT_OPERATOR_MARGIN_CAP_PCT
RISK_OVERRIDE_ENV = "LIVE_RISK_OVERRIDE_APPROVED"


def _risk_override_enabled() -> bool:
    """Return whether the operator REQUESTED an override (telemetry only).

    The value has no authority over new exposure; see module docstring.
    """
    return os.environ.get(RISK_OVERRIDE_ENV, "").strip().lower() == "true"


def _decision(drawdown):
    return risk_policy.drawdown_entry_decision(
        drawdown, cfg.MAX_DRAWDOWN, override_requested=_risk_override_enabled(),
    )


def _policy_violations() -> tuple[str, ...]:
    return risk_policy.load_policy(cfg).violations()


def _protect_drawdown_update(self, bound_update, log, *, source: str):
    """Wrap one balance update; a legacy drawdown pause is never neutralized."""
    async def _guarded_update(*args, **kwargs):
        was_active = bool(getattr(self, "active", False))
        risk_before = getattr(self, "risk", None)
        before = _decision(float(getattr(risk_before, "drawdown", 0.0) or 0.0))

        if was_active and not before.can_open:
            self._dd_alerted = True
            log.error(
                "[DRAWDOWN_HARD_GATE_%s] drawdown=%.2f%% configured_limit=%.2f%% "
                "override_requested=%s override_effect=NONE entries_blocked=true "
                "execution_effect=BLOCK_NEW_ENTRIES risk_reducing_actions=ALLOWED",
                source, before.drawdown * 100.0, before.limit * 100.0,
                str(before.override_requested).lower(),
            )

        try:
            return await bound_update(*args, **kwargs)
        finally:
            risk = getattr(self, "risk", None)
            after = _decision(float(getattr(risk, "drawdown", 0.0) or 0.0))
            became_inactive = was_active and not bool(getattr(self, "active", False))
            if became_inactive and not after.can_open:
                log.error(
                    "[DRAWDOWN_HARD_GATE_%s] drawdown=%.2f%% configured_limit=%.2f%% "
                    "legacy_pause_preserved=true active_restored=false "
                    "override_requested=%s override_effect=NONE",
                    source, after.drawdown * 100.0, after.limit * 100.0,
                    str(after.override_requested).lower(),
                )

    return _guarded_update


def _install_drawdown_advisory(TradingEngine_or_log, log=None) -> None:
    """Install the non-overridable drawdown hard gate.

    Backward compatible with the previous private helper signature
    ``_install_drawdown_advisory(log)`` used by regression tests. The engine
    wrappers are installed only when a TradingEngine class is explicitly passed.
    (The historical name is kept for the runtime contract; it is a hard gate.)
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
            violations = _policy_violations()
            if violations:
                log.critical(
                    "[RISK_POLICY_INVALID] entries_blocked=true violations=%s",
                    " | ".join(violations),
                )
                return False
            decision = _decision(self.drawdown)
            log.info(
                "[ENTRY_RISK_STATE] drawdown=%.2f%% configured_limit=%.2f%% "
                "entry_authorization=%s override_requested=%s override_effect=NONE",
                float(self.drawdown) * 100.0,
                float(cfg.MAX_DRAWDOWN) * 100.0,
                "ALLOW" if decision.can_open else "BLOCK_DRAWDOWN",
                str(decision.override_requested).lower(),
            )
            if not decision.can_open:
                log.error(
                    "[DRAWDOWN_HARD_GATE] drawdown=%.2f%% configured_limit=%.2f%% "
                    "reason=%s override_requested=%s override_effect=NONE entries_blocked=true",
                    float(self.drawdown) * 100.0,
                    float(cfg.MAX_DRAWDOWN) * 100.0,
                    decision.reason,
                    str(decision.override_requested).lower(),
                )
                return False
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
            if _policy_violations():
                return False
            decision = _decision(self.drawdown)
            if not decision.can_open:
                log.error(
                    "[DRAWDOWN_HARD_GATE_V3] drawdown=%.2f%% configured_limit=%.2f%% "
                    "reason=%s override_requested=%s override_effect=NONE entries_blocked=true",
                    float(self.drawdown) * 100.0,
                    float(cfg.MAX_DRAWDOWN) * 100.0,
                    decision.reason,
                    str(decision.override_requested).lower(),
                )
                return False
            if open_positions >= cfg.MAX_POSITIONS:
                return False
            return True

        RiskManagerV3.can_open = _v3_can_open
        RiskManagerV3._operator_drawdown_advisory = True

    if TradingEngine is None:
        return

    if not TradingEngine.__dict__.get("_operator_drawdown_engine_advisory", False):
        previous_update_balance = TradingEngine._update_balance

        async def _update_balance_advisory(self, *args, **kwargs):
            protected = _protect_drawdown_update(
                self,
                lambda *a, **k: previous_update_balance(self, *a, **k),
                log,
                source="ENGINE",
            )
            return await protected(*args, **kwargs)

        TradingEngine._update_balance = _update_balance_advisory
        TradingEngine._operator_drawdown_engine_advisory = True

    if (hasattr(TradingEngine, "run") and
            not TradingEngine.__dict__.get("_operator_drawdown_run_binding", False)):
        previous_run = TradingEngine.run

        async def _run_with_instance_drawdown_advisory(self, *args, **kwargs):
            if not getattr(self, "_operator_drawdown_instance_advisory", False):
                current_bound_update = self._update_balance
                self._update_balance = _protect_drawdown_update(
                    self,
                    current_bound_update,
                    log,
                    source="INSTANCE",
                )
                self._operator_drawdown_instance_advisory = True
                log.critical(
                    "[DRAWDOWN_POLICY_INSTANCE] installed=true class=%s "
                    "bound_update_module=%s bound_update_name=%s "
                    "default=hard_gate override_supported_for_new_entries=false",
                    type(self).__name__,
                    getattr(current_bound_update, "__module__", "unknown"),
                    getattr(current_bound_update, "__name__", type(current_bound_update).__name__),
                )
            watcher = startup_ready_notification.start(self, log)
            try:
                return await previous_run(self, *args, **kwargs)
            finally:
                await startup_ready_notification.cancel(watcher)

        TradingEngine.run = _run_with_instance_drawdown_advisory
        TradingEngine._operator_drawdown_run_binding = True


def install(TradingEngine, log) -> None:
    """Install the drawdown hard gate and report the canonical risk policy."""
    _install_drawdown_advisory(TradingEngine, log)
    policy = risk_policy.load_policy(cfg)
    violations = policy.violations()
    log.critical(
        "[OPERATOR_RUNTIME_POLICY] installed drawdown=hard_gate "
        "override_supported_for_new_entries=false override_requested=%s "
        "sizing_authority=risk_policy.size_new_entry operator_margin=CAP_ONLY "
        "policy_valid=%s %s ignored_overrides=%s railway_variables_unchanged=true",
        _risk_override_enabled(),
        str(not violations).lower(),
        policy.summary(),
        ",".join(policy.ignored_overrides) or "none",
    )
    for violation in violations:
        log.critical(
            "[RISK_POLICY_INVALID] violation=%s execution_effect=BLOCK_NEW_ENTRIES "
            "existing_positions=MANAGED",
            violation,
        )
