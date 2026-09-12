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


def evaluate_service_readiness(
    *,
    bootstrap_complete: bool,
    startup_blocked: bool,
    durable_state_ok: bool,
    instrument_count: int,
) -> ServiceReadiness:
    """Return process readiness without weakening any trading safety gate."""
    if not bootstrap_complete:
        return ServiceReadiness(False, "bootstrap_incomplete")
    if startup_blocked:
        return ServiceReadiness(False, "startup_blocked")
    if not durable_state_ok:
        return ServiceReadiness(False, "durable_state_unhealthy")
    if int(instrument_count or 0) <= 0:
        return ServiceReadiness(False, "instruments_unavailable")
    return ServiceReadiness(True, "service_healthy")
