"""Exchange mutation capability boundary.

READ_ONLY is a transport-level safety property: no authenticated or public
strategy path may mutate exchange state while this capability is selected.
"""
from __future__ import annotations

import os
from enum import Enum


class ExecutionCapability(str, Enum):
    LIVE = "LIVE"
    READ_ONLY = "READ_ONLY"


class ExchangeMutationBlocked(RuntimeError):
    pass


def current_execution_capability() -> ExecutionCapability:
    raw = os.environ.get("EXECUTION_CAPABILITY", "LIVE").strip().upper()
    try:
        return ExecutionCapability(raw)
    except ValueError as exc:
        raise RuntimeError(
            f"invalid EXECUTION_CAPABILITY={raw!r}; expected LIVE or READ_ONLY"
        ) from exc


def assert_exchange_mutation_allowed(method: str, endpoint: str) -> None:
    capability = current_execution_capability()
    if capability is not ExecutionCapability.LIVE:
        raise ExchangeMutationBlocked(
            f"SHADOW_CAN_NEVER_MUTATE_EXCHANGE capability={capability.value} "
            f"method={method.upper()} endpoint={endpoint}"
        )
