"""Distributed ownership fence for LIVE order dispatch.

This hardening does not change strategy, entry eligibility, leverage, sizing,
stop geometry or the operator's 50%-margin policy. It only prevents two
simultaneous process instances from both dispatching LIVE entry orders.

PostgreSQL session advisory locks are used deliberately: ownership follows the
actual DB session and is released automatically by PostgreSQL when that session
ends. PAPER remains completely unaffected.
"""
from __future__ import annotations

import asyncio

from bot import database as db

# Stable signed 64-bit advisory-lock key reserved for NEXUS-7 LIVE execution.
# It is intentionally code-owned rather than environment-configurable so two
# deployments cannot accidentally use different fencing namespaces.
_ADVISORY_LOCK_ID = 0x4E45585553370001
_guard = asyncio.Lock()
_owned_conn = None


def _connection_closed(conn) -> bool:
    checker = getattr(conn, "is_closed", None)
    if not callable(checker):
        return False
    try:
        return bool(checker())
    except Exception:
        return True


async def acquire(log) -> bool:
    """Return True only when this process owns the LIVE execution lease.

    The lock is acquired at most once per PostgreSQL connection. PostgreSQL
    advisory locks are re-entrant per session, so avoiding repeated acquisition
    prevents an accidental lock-count leak.
    """
    global _owned_conn

    async with _guard:
        conn = getattr(db, "_conn", None)
        is_pg = bool(getattr(db, "_is_pg", False))

        if not conn or not is_pg:
            _owned_conn = None
            log.critical(
                "[LIVE_EXECUTION_FENCE] owner=false reason=postgres_required"
            )
            return False

        if _owned_conn is conn and not _connection_closed(conn):
            return True

        _owned_conn = None
        try:
            # database.py serializes use of its single process connection.
            async with db._io_lock:
                acquired = bool(
                    await conn.fetchval(
                        "SELECT pg_try_advisory_lock($1)", _ADVISORY_LOCK_ID
                    )
                )
        except Exception as exc:
            log.critical(
                "[LIVE_EXECUTION_FENCE] owner=false reason=lock_error error=%s",
                type(exc).__name__,
            )
            return False

        if not acquired:
            log.critical(
                "[LIVE_EXECUTION_FENCE] owner=false "
                "reason=owned_by_other_instance dispatch=blocked"
            )
            return False

        _owned_conn = conn
        log.critical(
            "[LIVE_EXECUTION_FENCE] owner=true backend=postgres "
            "scope=live_place_order"
        )
        return True


def install(KuCoinClient, kucoin_module, log) -> None:
    """Wrap the final KuCoin order dispatcher with distributed ownership."""
    if getattr(KuCoinClient, "_live_execution_fence_installed", False):
        return

    previous_place_order = KuCoinClient.place_order

    async def _place_order_with_fence(self, *args, **kwargs):
        # PAPER semantics are intentionally byte-for-byte equivalent at the
        # behavioral boundary: no PostgreSQL ownership is required or queried.
        if bool(getattr(kucoin_module, "PAPER_TRADE", True)):
            return await previous_place_order(self, *args, **kwargs)

        if not await acquire(log):
            raise RuntimeError(
                "LIVE order dispatch rejected: execution ownership fence unavailable"
            )

        return await previous_place_order(self, *args, **kwargs)

    KuCoinClient.place_order = _place_order_with_fence
    KuCoinClient._live_execution_fence_installed = True
    log.info(
        "[LIVE_EXECUTION_FENCE] installed=true "
        "normal_owner_entries_unchanged=true paper_unchanged=true"
    )
