"""Deployment readiness independent from financial execution permission.

Railway may cut over to a candidate that is safely waiting for the previous
LIVE owner. This authority never grants permission to open/increase risk.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class EngineState(str, Enum):
    STARTING = "STARTING"
    WAITING_FOR_EXECUTION_OWNERSHIP = "WAITING_FOR_EXECUTION_OWNERSHIP"
    EXECUTION_OWNERSHIP_ACQUIRED = "EXECUTION_OWNERSHIP_ACQUIRED"
    CONNECTING = "CONNECTING"
    RECONCILING = "RECONCILING"
    PROTECTION_VALIDATING = "PROTECTION_VALIDATING"
    FINANCIAL_READY = "FINANCIAL_READY"
    FAILED = "FAILED"


@dataclass(frozen=True)
class DeploymentReadinessSnapshot:
    deployment_ready: bool
    reason: str
    engine_state: str


def engine_task_healthy(task, *, running: bool) -> bool:
    """A running worker is healthy; cancellation/failure is not."""
    if task is None:
        return False
    if not task.done():
        return True
    if task.cancelled():
        return False
    try:
        exc = task.exception()
    except (asyncio.CancelledError, Exception):
        return False
    return not running and exc is None


# Keep the import local footprint tiny; this module is imported at startup.
import asyncio


def evaluate_deployment_readiness(
    *,
    bootstrap_complete: bool,
    startup_blocked: bool,
    durable_state_ok: bool,
    instrument_count: int,
    worker_healthy: bool,
    engine_state: str,
    db_authority_valid: bool = True,
) -> DeploymentReadinessSnapshot:
    """Prove safe cutover readiness without granting trading authority."""
    state = str(engine_state or EngineState.STARTING.value)
    if state == EngineState.FAILED.value:
        return DeploymentReadinessSnapshot(False, "engine_failed", state)
    if not bootstrap_complete:
        return DeploymentReadinessSnapshot(False, "bootstrap_incomplete", state)
    if startup_blocked:
        return DeploymentReadinessSnapshot(False, "startup_blocked", state)
    if not worker_healthy:
        return DeploymentReadinessSnapshot(False, "engine_task_unhealthy", state)
    if not db_authority_valid:
        return DeploymentReadinessSnapshot(False, "db_authority_invalid", state)
    if not durable_state_ok:
        return DeploymentReadinessSnapshot(False, "durable_state_unhealthy", state)
    if int(instrument_count or 0) <= 0:
        return DeploymentReadinessSnapshot(False, "instruments_unavailable", state)

    safe_states = {
        EngineState.WAITING_FOR_EXECUTION_OWNERSHIP.value,
        EngineState.EXECUTION_OWNERSHIP_ACQUIRED.value,
        EngineState.CONNECTING.value,
        EngineState.RECONCILING.value,
        EngineState.PROTECTION_VALIDATING.value,
        EngineState.FINANCIAL_READY.value,
    }
    if state not in safe_states:
        return DeploymentReadinessSnapshot(False, "engine_not_cutover_safe", state)
    return DeploymentReadinessSnapshot(True, "deployment_safe", state)


# Compatibility alias for existing tests/importers. Semantics are deployment-only.
ServiceReadiness = DeploymentReadinessSnapshot


def evaluate_service_readiness(
    *,
    bootstrap_complete: bool,
    startup_blocked: bool,
    durable_state_ok: bool,
    instrument_count: int,
    worker_healthy: bool = True,
):
    return evaluate_deployment_readiness(
        bootstrap_complete=bootstrap_complete,
        startup_blocked=startup_blocked,
        durable_state_ok=durable_state_ok,
        instrument_count=instrument_count,
        worker_healthy=worker_healthy,
        engine_state=EngineState.WAITING_FOR_EXECUTION_OWNERSHIP.value,
    )
