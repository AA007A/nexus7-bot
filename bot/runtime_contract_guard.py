"""Fail-closed contract guard for the final NEXUS-7 runtime patch stack.

The project intentionally composes a number of narrowly-scoped runtime wrappers.
That makes installation order operationally significant: a later wrapper can
silently replace an earlier safety or operator-policy method while every module
still imports successfully.

This guard runs *last* in ``runtime_overlays.install`` and proves ownership of a
small set of execution-critical final callables. It does not change thresholds,
leverage, sizing, drawdown semantics or entry authorization. It only refuses a
startup whose final runtime graph is not the one that was explicitly reviewed.
"""
from __future__ import annotations

import builtins
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ContractItem:
    name: str
    value: Any
    expected_module: str


def callable_owner(value: Any) -> str:
    return str(getattr(value, "__module__", "") or "")


def verify(items: tuple[ContractItem, ...]) -> tuple[bool, tuple[str, ...]]:
    errors: list[str] = []
    for item in items:
        owner = callable_owner(item.value)
        if owner != item.expected_module:
            errors.append(
                f"{item.name}: owner={owner or '<unknown>'} "
                f"expected={item.expected_module}"
            )
    return (not errors, tuple(errors))


def install(TradingEngine, PilotGuard, nexus_ai, engine_module, log) -> None:
    """Prove the final reviewed runtime graph; fail closed on ownership drift."""
    if getattr(builtins, "_nexus_runtime_contract_status", None) == "ok":
        return

    from bot.risk import RiskManager
    from bot.risk_manager_v3 import RiskManagerV3

    items = (
        ContractItem(
            "TradingEngine.run",
            TradingEngine.run,
            "bot.operator_runtime_policy",
        ),
        ContractItem(
            "TradingEngine._update_balance",
            TradingEngine._update_balance,
            "bot.operator_runtime_policy",
        ),
        ContractItem(
            "RiskManager.can_open",
            RiskManager.can_open,
            "bot.operator_runtime_policy",
        ),
        ContractItem(
            "RiskManagerV3.can_open",
            RiskManagerV3.can_open,
            "bot.operator_runtime_policy",
        ),
        ContractItem(
            "engine.minimum_base_quantity",
            engine_module.minimum_base_quantity,
            "bot.operator_runtime_policy",
        ),
        ContractItem(
            "PilotGuard.evaluate",
            PilotGuard.evaluate,
            "bot.pilot_exposure_capacity",
        ),
        ContractItem(
            "nexus_ai.regime_compatibility",
            nexus_ai.regime_compatibility,
            "bot.nexus_regime_transition_consistency",
        ),
    )

    ok, errors = verify(items)
    if not ok:
        builtins._nexus_runtime_contract_status = "failed"
        detail = "; ".join(errors)
        log.critical(
            "[RUNTIME_CONTRACT] status=FAILED action=BLOCK_STARTUP "
            "reason=late_wrapper_drift detail=%s",
            detail,
        )
        raise RuntimeError(f"RUNTIME_CONTRACT_DRIFT: {detail}")

    builtins._nexus_runtime_contract_status = "ok"
    builtins._nexus_runtime_contract_snapshot = {
        item.name: callable_owner(item.value) for item in items
    }
    log.critical(
        "[RUNTIME_CONTRACT] status=PASS protected_callables=%d "
        "late_wrapper_drift=false leverage_unchanged=true sizing_unchanged=true "
        "drawdown_policy_unchanged=true entry_authorization_unchanged=true",
        len(items),
    )
