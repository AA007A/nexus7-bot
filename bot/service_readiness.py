"""Infrastructure readiness independent from trading permission.

Railway needs to know whether the process is healthy enough to serve and keep
reconciling state. A deliberate trading safety pause (drawdown, integrity gate,
or another fail-closed control) must not make the container unhealthy and
therefore trigger a deployment rollback/restart loop.

This module deliberately does not inspect ``engine.active`` or any execution
gate. Trading readiness remains owned by ``runtime_mode_observability``.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ServiceReadiness:
    ready: bool
    reason: str


def engine_task_healthy(task, *, running: bool) -> bool:
    """A deliberate clean pause is healthy; a lost/failed worker is not."""
    if task is None:
        return False
    if not task.done():
        return True
    if task.cancelled():
        return False
    return not running and task.exception() is None


def evaluate_service_readiness(
    *,
    bootstrap_complete: bool,
    startup_blocked: bool,
    durable_state_ok: bool,
    instrument_count: int,
    worker_healthy: bool = True,
) -> ServiceReadiness:
    """Return process readiness without weakening any trading safety gate."""
    if not bootstrap_complete:
        return ServiceReadiness(False, "bootstrap_incomplete")
    if startup_blocked:
        return ServiceReadiness(False, "startup_blocked")
    if not worker_healthy:
        return ServiceReadiness(False, "engine_task_unhealthy")
    if not durable_state_ok:
        return ServiceReadiness(False, "durable_state_unhealthy")
    if int(instrument_count or 0) <= 0:
        return ServiceReadiness(False, "instruments_unavailable")
    return ServiceReadiness(True, "service_healthy")
