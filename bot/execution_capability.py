"""Exchange mutation capability barrier.

This is intentionally enforced at the transport boundary.  READ_ONLY runtimes
may perform public/private reads, but can never mutate exchange state even if
higher layers incorrectly authorize execution.
"""
from __future__ import annotations

import os
from enum import Enum


class ExecutionCapability(str, Enum):
    LIVE = "LIVE"
    READ_ONLY = "READ_ONLY"


class ExchangeMutationBlocked(PermissionError):
    pass


_READ_ONLY_POST_ENDPOINTS = frozenset({
    "/api/v1/bullet-public",
    "/api/v1/bullet-private",
    "/api/v2/batchGetCrossOrderLimit",
})


def configured_capability() -> ExecutionCapability:
    raw = os.environ.get("EXECUTION_CAPABILITY", "LIVE").strip().upper()
    try:
        return ExecutionCapability(raw)
    except ValueError as exc:
        raise RuntimeError(f"invalid EXECUTION_CAPABILITY={raw!r}") from exc


def assert_post_allowed(endpoint: str) -> None:
    if configured_capability() is ExecutionCapability.LIVE:
        return
    if endpoint in _READ_ONLY_POST_ENDPOINTS:
        return
    raise ExchangeMutationBlocked(
        f"SHADOW_CAN_NEVER_MUTATE_EXCHANGE: POST {endpoint} blocked by READ_ONLY capability"
    )
