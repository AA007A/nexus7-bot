"""Bounded continuous reconciliation for non-terminal durable LIVE orders.

This is a lifecycle-only companion to ``durable_execution.reconcile_orders``.
It reuses the same authoritative KuCoin byClientOid truth source, never
resubmits an order, and never infers terminal execution from position state.
"""
from __future__ import annotations

import math
import time

from bot import durable_execution as durable
from bot.logger import log
from bot.order_state import OrderState


_MIN_INTERVAL_S = 10.0


def _canon_symbol(value: str) -> str:
    symbol = str(value or "").upper()
    if symbol == "XBTUSDTM":
        return "BTCUSDT"
    if symbol.endswith("USDTM"):
        return f"{symbol[:-5]}USDT"
    return symbol


async def _owner_valid(engine) -> bool:
    if getattr(engine, "paper_trade", False):
        return False
    if not bool(getattr(engine, "_execution_ownership_valid", False)):
        return False
    client = getattr(engine, "client", None)
    raw_client = getattr(client, "_client", client)
    ownership = getattr(raw_client, "_execution_ownership", None)
    if ownership is None:
        ownership = getattr(client, "_execution_ownership", None)
    if ownership is None:
        return False
    try:
        from bot.execution_ownership import validate_execution_ownership
        await validate_execution_ownership(ownership)
        return True
    except Exception as exc:
        log.warning(
            "[DURABLE_LIVE_RECONCILE] ownership_valid=false error=%s",
            type(exc).__name__,
        )
        return False


def _transition_from_truth(engine, order, data: dict) -> tuple[bool, bool]:
    """Apply one authoritative REST snapshot.

    Returns ``(changed, terminal)``.  The durable requested quantity is stored
    in KuCoin contract units, matching ``filledSize``/``dealSize``.
    """
    if not isinstance(data, dict) or not data:
        return False, False

    remote_client_oid = str(data.get("clientOid") or "")
    if remote_client_oid and remote_client_oid != order.client_oid:
        raise ValueError("durable/exchange clientOid mismatch")

    remote_symbol = str(data.get("symbol") or "")
    if remote_symbol and _canon_symbol(remote_symbol) != _canon_symbol(order.symbol):
        raise ValueError("durable/exchange symbol mismatch")

    remote_order_id = str(data.get("orderId") or data.get("id") or "")
    if order.order_id and remote_order_id and order.order_id != remote_order_id:
        raise ValueError("durable/exchange orderId mismatch")
    if remote_order_id:
        engine.orders.index_order_id(remote_order_id, order.client_oid)

    requested = float(order.qty)
    filled = float(data.get("filledSize", data.get("dealSize", 0)) or 0)
    if not all(math.isfinite(value) and value >= 0 for value in (requested, filled)):
        raise ValueError("non-finite durable order quantity")
    if requested <= 0:
        raise ValueError("invalid durable requested quantity")

    active = bool(data.get("isActive", False))
    status = str(data.get("status") or "").strip().lower()
    cancel_exists = bool(data.get("cancelExist", False))
    tolerance = max(1e-9, requested * 1e-9)
    changed = False

    # Preserve normal monotonic progression while the exchange still considers
    # the order active. This is state reconciliation only; no accounting side
    # effect is invoked here.
    if active:
        if order.state == OrderState.SUBMITTING:
            order.transition(
                OrderState.SUBMITTED,
                order_id=remote_order_id,
                source="LIVE_REST",
            )
            changed = True
        if filled > 0 and order.state == OrderState.SUBMITTED:
            order.transition(
                OrderState.PARTIALLY_FILLED,
                order_id=remote_order_id,
                filled_qty=filled,
                source="LIVE_REST",
            )
            changed = True
        elif filled > 0 and order.state == OrderState.PARTIALLY_FILLED:
            before = float(order.filled_qty or 0)
            if filled > before + tolerance:
                order.transition(
                    OrderState.PARTIALLY_FILLED,
                    order_id=remote_order_id,
                    filled_qty=filled,
                    source="LIVE_REST",
                )
                changed = True
        return changed, order.is_terminal

    # Inactive/completed is not automatically FILLED. A full terminal fill must
    # cover the durable requested contract quantity. This specifically avoids
    # classifying a partial execution + cancelled remainder as a full fill.
    if filled + tolerance >= requested:
        if order.state == OrderState.SUBMITTING:
            order.transition(
                OrderState.SUBMITTED,
                order_id=remote_order_id,
                source="LIVE_REST",
            )
            changed = True
        if not order.is_terminal:
            order.transition(
                OrderState.FILLED,
                order_id=remote_order_id,
                filled_qty=filled,
                source="LIVE_REST",
            )
            changed = True
        return changed, True

    # An inactive order with less than requested execution is terminal but not a
    # full fill. KuCoin uses cancelExist/status to expose cancellation, while a
    # done inactive order with partial size is equivalent for lifecycle purposes.
    if status in {"rejected", "reject"} and order.state != OrderState.PARTIALLY_FILLED:
        if order.state == OrderState.SUBMITTING:
            order.transition(OrderState.REJECTED, source="LIVE_REST")
        elif order.state == OrderState.SUBMITTED:
            order.transition(OrderState.REJECTED, source="LIVE_REST")
        changed = True
        return changed, True

    if cancel_exists or status in {"cancelled", "canceled", "done"} or filled < requested:
        if order.state == OrderState.SUBMITTING:
            order.transition(
                OrderState.SUBMITTED,
                order_id=remote_order_id,
                source="LIVE_REST",
            )
            changed = True
        if not order.is_terminal:
            order.transition(
                OrderState.CANCELLED,
                order_id=remote_order_id,
                filled_qty=filled,
                source="LIVE_REST",
            )
            changed = True
        return changed, True

    return changed, order.is_terminal


async def reconcile_pending(engine, *, min_interval_s: float = _MIN_INTERVAL_S) -> bool:
    """Continuously converge only non-terminal ManagedOrders from exchange truth.

    Request rate is bounded to at most one byClientOid lookup per currently
    pending order per cadence on the execution owner. Terminal history is never
    polled. No order submission, fill accounting, PnL, or trade-row mutation is
    performed here.
    """
    registry = getattr(engine, "orders", None)
    pending_reader = getattr(registry, "pending_orders", None)
    if not callable(pending_reader):
        return False
    pending = list(pending_reader() or [])
    if not pending:
        return True
    if not getattr(engine, "connected", False):
        return False
    if not await _owner_valid(engine):
        return False

    now = time.monotonic()
    last = float(getattr(engine, "_durable_live_reconcile_last", 0.0) or 0.0)
    if now - last < max(1.0, float(min_interval_s)):
        return False
    engine._durable_live_reconcile_last = now

    lookup = getattr(getattr(engine, "client", None), "get_order_by_client_oid", None)
    if not callable(lookup):
        durable._block(engine, "orders")
        return False

    changed = 0
    unresolved = []
    for order in list(pending):
        if order.is_terminal:
            continue
        try:
            data = await lookup(order.client_oid)
            if not data:
                unresolved.append(order.client_oid)
                continue
            before = order.state
            did_change, terminal = _transition_from_truth(engine, order, data)
            changed += int(did_change)
            if not terminal:
                unresolved.append(order.client_oid)
            log.info(
                "[DURABLE_LIVE_RECONCILE] client_oid=%s symbol=%s before=%s after=%s "
                "terminal=%s mutation=STATE_ONLY resubmit=false accounting_side_effect=false",
                order.client_oid,
                order.symbol,
                before.value,
                order.state.value,
                str(order.is_terminal).lower(),
            )
        except Exception as exc:
            unresolved.append(order.client_oid)
            log.error(
                "[DURABLE_LIVE_RECONCILE] client_oid=%s failed=%s",
                order.client_oid,
                type(exc).__name__,
            )

    if changed:
        if not await durable.persist_orders(
            engine, "continuous_exchange_truth_reconcile", strict=True
        ):
            durable._block(engine, "orders")
            return False

    remaining = list(pending_reader() or [])
    if remaining or unresolved:
        durable._block(engine, "orders")
        return False

    durable._clear(engine, "orders")
    return True
