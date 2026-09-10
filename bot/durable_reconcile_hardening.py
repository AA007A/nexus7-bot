"""Startup reconciliation hardening for durable LIVE order intents.

This module does not submit, cancel, amend, or retry exchange orders. It only
improves recovery of already-persisted order intents after restart.

Safety invariants:
- CREATED means the durable intent was persisted before network dispatch; on
  restart it is safe to terminalize as FAILED because no submission began.
- SUBMITTING/SUBMITTED/PARTIALLY_FILLED remain fail-closed unless exchange
  evidence proves a terminal state.
- If an order_id is already durable, prefer the authoritative order-id lookup
  before falling back to the legacy clientOid reconciliation path.
- Never infer "not submitted" merely from an empty/failed exchange read.
"""
from __future__ import annotations

import time


def install(durable_module, order_state_module, log) -> None:
    if getattr(durable_module, "_startup_reconcile_hardening_installed", False):
        return

    original = durable_module.reconcile_orders
    OrderState = order_state_module.OrderState

    async def reconcile_orders_hardened(engine) -> bool:
        # Run the established reconciliation first. It remains the primary path.
        ok = await original(engine)
        if ok:
            return True

        pending = list(engine.orders.pending_orders())
        if not pending:
            durable_module._clear(engine, "orders")
            return await durable_module.persist_orders(
                engine, "startup_reconcile_hardened_empty", strict=True
            )

        changed = False
        for order in pending:
            age_s = max(0.0, time.time() - float(order.created_at or time.time()))
            log.warning(
                "[DURABLE_RECONCILE_DETAIL] clientOid=%s symbol=%s state=%s "
                "orderId=%s age_s=%.1f",
                order.client_oid, order.symbol, order.state.value,
                order.order_id or "NONE", age_s,
            )

            # CREATED is pre-dispatch by construction: durable intent exists,
            # but the submission transition has not started. Restart may safely
            # close it without exchange mutation or any retry.
            if order.state == OrderState.CREATED:
                order.transition(
                    OrderState.FAILED,
                    source="STARTUP_NEVER_DISPATCHED",
                    reason="restored_created_intent",
                )
                changed = True
                log.warning(
                    "[DURABLE_RECONCILE] terminalized pre-dispatch intent "
                    "clientOid=%s symbol=%s CREATED->FAILED execution_effect=NONE",
                    order.client_oid, order.symbol,
                )
                continue

            # If the exact exchange order id is already durable, use it. This
            # covers cases where clientOid lookup is unavailable/stale while the
            # canonical order record is still queryable by order id.
            if order.order_id and order.state in (
                OrderState.SUBMITTING,
                OrderState.SUBMITTED,
                OrderState.PARTIALLY_FILLED,
            ):
                try:
                    status = await engine.client.get_order_status(order.order_id)
                except Exception as exc:
                    log.error(
                        "[DURABLE_RECONCILE] orderId lookup failed orderId=%s: %s",
                        order.order_id, exc,
                    )
                    status = {}

                if isinstance(status, dict) and status and not status.get("_unknown"):
                    try:
                        filled = float(status.get("filledSize", status.get("dealSize", 0)) or 0)
                    except (TypeError, ValueError):
                        filled = 0.0
                    active = bool(status.get("isActive", True))
                    cancelled = bool(status.get("cancelExist", False))

                    if filled > 0 and not active and not cancelled:
                        durable_module._advance(
                            order,
                            OrderState.FILLED,
                            order_id=order.order_id,
                            filled_qty=filled,
                            source="STARTUP_ORDER_ID",
                        )
                        changed = True
                        log.warning(
                            "[DURABLE_RECONCILE] resolved by orderId clientOid=%s "
                            "orderId=%s state=FILLED filled=%s execution_effect=NONE",
                            order.client_oid, order.order_id, filled,
                        )
                        continue

                    if not active and filled <= 0:
                        target = (
                            OrderState.REJECTED
                            if order.state == OrderState.SUBMITTING
                            else OrderState.CANCELLED
                        )
                        order.transition(
                            target,
                            source="STARTUP_ORDER_ID",
                            order_id=order.order_id,
                        )
                        changed = True
                        log.warning(
                            "[DURABLE_RECONCILE] resolved by orderId clientOid=%s "
                            "orderId=%s state=%s execution_effect=NONE",
                            order.client_oid, order.order_id, target.value,
                        )
                        continue

        if changed:
            saved = await durable_module.persist_orders(
                engine, "startup_reconcile_hardened", strict=True
            )
            if not saved:
                return False

        remaining = list(engine.orders.pending_orders())
        if remaining:
            durable_module._block(engine, "orders")
            log.critical(
                "[DURABLE_RECONCILE] remaining_unresolved=%s; fail_closed=true; "
                "no retry sent",
                len(remaining),
            )
            return False

        durable_module._clear(engine, "orders")
        log.warning(
            "[DURABLE_RECONCILE] all restored intents resolved; new entries may "
            "proceed subject to normal gates execution_effect=NONE"
        )
        return True

    durable_module.reconcile_orders = reconcile_orders_hardened
    durable_module._startup_reconcile_hardening_installed = True
    log.info(
        "[DURABLE_RECONCILE] startup hardening installed: pre-dispatch CREATED "
        "terminalization + orderId-first recovery; no exchange mutations"
    )
