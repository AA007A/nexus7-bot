"""Canonical startup reconciliation authority for runtime readiness.

Durable storage health is intentionally not an input that can complete this
gate.  Completion requires fresh, read-only exchange truth plus explicit
position/order attribution and protection inspection.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from bot.conditional_stop_protection import conditional_stop_confirmed
from bot.logger import log
from bot.protection_readiness import (
    publish_exchange_protection_state,
    reset_protection_readiness,
)

DURABLE_STATE_READY_IS_NOT_RECONCILIATION_COMPLETE = True


@dataclass(frozen=True)
class InitialReconciliationReceipt:
    positions: int
    active_orders: int
    bgx_positions: int
    external_positions: int
    bgx_active_orders: int
    external_active_orders: int
    protected_positions: int
    unprotected_positions: int


def begin(engine) -> None:
    """Reset the gate before any startup I/O can be treated as evidence."""
    engine._initial_reconciliation_complete = False
    engine._initial_reconciliation_receipt = None
    reset_protection_readiness(engine)


def _active_orders(payload) -> list[dict]:
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        items = None
        for key in ("items", "dataList", "orders"):
            candidate = payload.get(key)
            if isinstance(candidate, list):
                items = candidate
                break
        if items is None and isinstance(payload.get("data"), list):
            items = payload["data"]
        if items is None:
            raise ValueError("active-order response has no list payload")
    else:
        raise ValueError("active-order response is not a collection")
    if not all(isinstance(item, dict) for item in items):
        raise ValueError("active-order response contains a non-object item")
    return list(items)


def _live_positions(payload) -> list[dict]:
    if not isinstance(payload, list):
        raise ValueError("position response is not a list")
    live = []
    for item in payload:
        if not isinstance(item, dict):
            raise ValueError("position response contains a non-object item")
        try:
            size = abs(float(item.get("size", 0) or 0))
        except (TypeError, ValueError) as exc:
            raise ValueError("position size is not numeric") from exc
        if size <= 0:
            continue
        if not str(item.get("symbol", "") or ""):
            raise ValueError("live position has no symbol")
        live.append(item)
    return live


def _order_is_bgx(engine, order: dict) -> bool:
    client_oid = str(order.get("clientOid", order.get("clientOrderId", "")) or "")
    order_id = str(order.get("orderId", order.get("id", "")) or "")
    managed = engine.orders.get(client_oid) if client_oid else None
    if managed is None and order_id:
        managed = engine.orders.get_by_order_id(order_id)
    if managed is None:
        return False
    if client_oid and managed.client_oid != client_oid:
        return False
    if order_id and managed.order_id and managed.order_id != order_id:
        return False
    return True


async def reconcile_initial_state(engine, *, durable_orders_reconciled: bool) -> bool:
    """Publish completion only after a fresh fail-closed reconciliation."""
    begin(engine)
    try:
        if durable_orders_reconciled is not True:
            raise RuntimeError("durable order reconciliation incomplete")
        if not bool(getattr(engine, "connected", False)):
            raise RuntimeError("exchange is not connected")

        positions = _live_positions(await engine.client.get_positions())
        orders_raw = await engine.client._get(
            "/api/v1/orders", {"status": "active"}, auth=True
        )
        active_orders = _active_orders(orders_raw)

        live_symbols = {str(position["symbol"]) for position in positions}
        if getattr(engine, "paper_trade", False):
            bgx_symbols = set(live_symbols)
            external_symbols: set[str] = set()
        else:
            bgx_symbols = set(
                getattr(engine, "_recovered_position_symbols", set()) or set()
            )
            external_symbols = set(
                getattr(engine, "_external_position_symbols", set()) or set()
            )
            if bgx_symbols & external_symbols:
                raise RuntimeError("position attribution overlaps")
            if live_symbols != bgx_symbols | external_symbols:
                raise RuntimeError("position attribution is incomplete")

        protected = 0
        unprotected = 0
        protection_evidence = {}
        protection_readbacks = {}
        for position in positions:
            confirmed, evidence = await conditional_stop_confirmed(
                engine.client, position
            )
            symbol = str(position["symbol"])
            protection_evidence[symbol] = str(evidence)
            protection_readbacks[symbol] = (confirmed, str(evidence))
            if confirmed is True:
                protected += 1
            else:
                unprotected += 1

        bgx_orders = sum(_order_is_bgx(engine, order) for order in active_orders)
        receipt = InitialReconciliationReceipt(
            positions=len(positions),
            active_orders=len(active_orders),
            bgx_positions=len(bgx_symbols),
            external_positions=len(external_symbols),
            bgx_active_orders=bgx_orders,
            external_active_orders=len(active_orders) - bgx_orders,
            protected_positions=protected,
            unprotected_positions=unprotected,
        )
        engine._initial_reconciliation_receipt = {
            **asdict(receipt),
            "position_attribution_complete": True,
            "active_order_attribution_complete": True,
            "protection_evidence": protection_evidence,
        }
        publish_exchange_protection_state(
            engine, positions, protection_readbacks
        )
        engine._initial_reconciliation_complete = True
        log.info(
            "[INITIAL_RECONCILIATION] result=COMPLETE positions=%s "
            "active_orders=%s bgx_positions=%s external_positions=%s "
            "bgx_active_orders=%s external_active_orders=%s protected=%s "
            "unprotected=%s",
            receipt.positions,
            receipt.active_orders,
            receipt.bgx_positions,
            receipt.external_positions,
            receipt.bgx_active_orders,
            receipt.external_active_orders,
            receipt.protected_positions,
            receipt.unprotected_positions,
        )
        return True
    except Exception as exc:
        engine._initial_reconciliation_complete = False
        engine._initial_reconciliation_receipt = None
        log.critical(
            "[INITIAL_RECONCILIATION] result=INCOMPLETE reason=%s "
            "execution_effect=BLOCK_NEW_ENTRIES",
            type(exc).__name__,
        )
        return False
