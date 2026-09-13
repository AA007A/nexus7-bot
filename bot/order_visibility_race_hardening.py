"""Reduce false KuCoin order-not-found noise during WS/REST visibility races.

Reconciliation/observability hardening only. Native order submission, SL/TP,
sizing, leverage and execution permissions remain unchanged. When the private
order websocket has already observed an order as PARTIALLY_FILLED/FILLED, give
KuCoin REST a short indexing grace window before the normal authoritative
``wait_for_fill`` poll. The grace is deducted from the caller's timeout budget.
"""
from __future__ import annotations

import asyncio
import time

WS_AHEAD_GRACE_S = 0.85
POLL_S = 0.05
FILLED_SETTLE_S = 0.10
MIN_REST_TIMEOUT_S = 0.10


def _state_value(order) -> str:
    state = getattr(order, "state", None)
    return str(getattr(state, "value", state) or "")


def install(KuCoinClient, log) -> None:
    if getattr(KuCoinClient, "_order_visibility_race_hardening_installed", False):
        return

    original_wait_for_fill = KuCoinClient.wait_for_fill

    async def wait_for_fill_hardened(self, order_id: str, timeout_s: float = 8.0,
                                     poll_interval_s: float = 0.5):
        requested_timeout = max(0.0, float(timeout_s))
        started = time.monotonic()
        registry = getattr(self, "_order_registry", None)
        managed = None
        try:
            if registry is not None and order_id:
                managed = registry.get_by_order_id(str(order_id))
        except Exception:
            managed = None

        initial_state = _state_value(managed)
        if initial_state in ("PARTIALLY_FILLED", "FILLED") and requested_timeout > MIN_REST_TIMEOUT_S:
            max_grace = min(
                WS_AHEAD_GRACE_S,
                max(0.0, requested_timeout - MIN_REST_TIMEOUT_S),
            )
            grace_deadline = time.monotonic() + max_grace
            while time.monotonic() < grace_deadline:
                current = _state_value(managed)
                if current == "FILLED":
                    settle = min(
                        FILLED_SETTLE_S,
                        max(0.0, grace_deadline - time.monotonic()),
                    )
                    if settle > 0:
                        await asyncio.sleep(settle)
                    break
                await asyncio.sleep(min(POLL_S, max(0.0, grace_deadline - time.monotonic())))

            elapsed = max(0.0, time.monotonic() - started)
            log.info(
                "[ORDER_VISIBILITY_RACE] orderId=%s initial_ws_state=%s final_ws_state=%s "
                "grace_ms=%d action=REST_CONFIRMATION_DELAYED execution_effect=NONE",
                order_id, initial_state, _state_value(managed), int(elapsed * 1000),
            )
        else:
            elapsed = max(0.0, time.monotonic() - started)

        remaining_timeout = max(
            MIN_REST_TIMEOUT_S,
            requested_timeout - elapsed,
        ) if requested_timeout > 0 else 0.0

        return await original_wait_for_fill(
            self, order_id,
            timeout_s=remaining_timeout,
            poll_interval_s=poll_interval_s,
        )

    KuCoinClient.wait_for_fill = wait_for_fill_hardened
    KuCoinClient._order_visibility_race_hardening_installed = True
    log.info(
        "[ORDER_VISIBILITY_RACE] installed=true private_ws_grace_ms=%d "
        "timeout_budget_preserved=true rest_confirmation_preserved=true execution_effect=NONE",
        int(WS_AHEAD_GRACE_S * 1000),
    )
