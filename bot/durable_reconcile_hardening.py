"""Startup reconciliation hardening for durable LIVE order intents.

This module never submits, cancels, amends or retries exchange orders. It only
uses authenticated read evidence to resolve already-persisted order intents.
"""
from __future__ import annotations

import time

STALE_SUBMITTING_AGE_S = 300.0


def _active_items(payload):
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        items = payload.get("items") or payload.get("data") or payload.get("orders") or []
        return [x for x in items if isinstance(x, dict)] if isinstance(items, list) else []
    return None


async def _prove_absent_and_flat(engine, order, log) -> bool:
    """Return True only after three independent authenticated read checks.

    1) exact clientOid lookup is absent;
    2) account has no open positions;
    3) account has no active orders.

    Any exception or malformed payload fails closed. This proves only that the
    stale intent is not live *now*; it does not rewrite historical PnL.
    """
    try:
        by_oid = await engine.client.get_order_by_client_oid(order.client_oid)
        if by_oid:
            return False
        positions = await engine.client.get_positions()
        if not isinstance(positions, list):
            return False
        if any(abs(float((p or {}).get("size", 0) or 0)) > 0 for p in positions if isinstance(p, dict)):
            return False
        active = await engine.client._get("/api/v1/orders", {"status": "active"}, auth=True)
        items = _active_items(active)
        if items is None or items:
            return False
        return True
    except Exception as exc:
        log.error(
            "[DURABLE_RECONCILE] flat-proof failed clientOid=%s: %s",
            order.client_oid, exc,
        )
        return False


def install(durable_module, order_state_module, log) -> None:
    if getattr(durable_module, "_startup_reconcile_hardening_installed", False):
        return

    original = durable_module.reconcile_orders
    OrderState = order_state_module.OrderState

    async def reconcile_orders_hardened(engine) -> bool:
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
                            order, OrderState.FILLED,
                            order_id=order.order_id,
                            filled_qty=filled,
                            source="STARTUP_ORDER_ID",
                        )
                        changed = True
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
                        continue

            # A SUBMITTING intent without orderId is ambiguous immediately after
            # a crash. After a conservative age, it may be terminalized only if
            # the exact clientOid is absent AND the whole account is flat with no
            # active exchange orders. Any uncertain read remains fail-closed.
            if (
                order.state == OrderState.SUBMITTING
                and not order.order_id
                and age_s >= STALE_SUBMITTING_AGE_S
                and await _prove_absent_and_flat(engine, order, log)
            ):
                order.transition(
                    OrderState.FAILED,
                    source="STARTUP_STALE_ABSENT_FLAT",
                    reason="client_oid_absent_account_flat_no_active_orders",
                )
                changed = True
                log.warning(
                    "[DURABLE_RECONCILE] stale intent terminalized clientOid=%s "
                    "symbol=%s SUBMITTING->FAILED age_s=%.1f proof=absent+flat+no_active "
                    "execution_effect=NONE",
                    order.client_oid, order.symbol, age_s,
                )

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
                "[DURABLE_RECONCILE] remaining_unresolved=%s; fail_closed=true; no retry sent",
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
        "terminalization + orderId recovery + stale absent/flat proof; no exchange mutations"
    )
