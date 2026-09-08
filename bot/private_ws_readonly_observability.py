"""Read-only private/account readiness observability for SHADOW LIVE.

This module never submits/cancels orders, changes leverage/stops, or grants
execution permission. It performs authenticated read-only KuCoin checks for
private WS subscription, live positions, and active orders, then stores only
diagnostic state on the client instance.
"""

import asyncio


def _first(order, *keys, default=""):
    """Return the first non-empty value across KuCoin field-name variants."""
    for key in keys:
        value = order.get(key)
        if value not in (None, ""):
            return value
    return default


def _log_active_order_forensics(orders, log):
    """Emit normalized, non-secret details for active orders in SHADOW only."""
    for idx, order in enumerate(orders or [], start=1):
        if not isinstance(order, dict):
            log.warning(
                "[PRELIVE_ACTIVE_ORDER] index=%s result=UNPARSEABLE execution_effect=NONE",
                idx,
            )
            continue
        log.warning(
            "[PRELIVE_ACTIVE_ORDER] index=%s symbol=%s side=%s type=%s status=%s "
            "price=%s size=%s filled=%s orderId=%s clientOid=%s createdAt=%s "
            "source=KuCoin read_only=true execution_effect=NONE",
            idx,
            _first(order, "symbol", "contract", default="?"),
            _first(order, "side", default="?"),
            _first(order, "type", "orderType", default="?"),
            _first(order, "status", default="active"),
            _first(order, "price", "orderPrice", default="0"),
            _first(order, "size", "orderSize", "qty", default="0"),
            _first(order, "filledSize", "dealSize", "filledQty", default="0"),
            _first(order, "orderId", "id", default="?"),
            _first(order, "clientOid", "clientOrderId", default="?"),
            _first(order, "createdAt", "createdTime", "ts", default="?"),
        )


async def run(client, instruments, log) -> bool:
    from bot.prelive_readonly_probe import _active_orders, _private_ws_probe

    symbol = "ETHUSDT" if "ETHUSDT" in instruments else next(iter(instruments), "")
    if not symbol:
        setattr(client, "_prelive_private_ws_probe_ok", False)
        setattr(client, "_prelive_account_exposure_verified", False)
        setattr(client, "_prelive_account_exposure_clear", False)
        log.warning(
            "[PRIVATE_WS_READONLY_PROBE] result=FAIL reason=no_instrument execution_effect=NONE"
        )
        return False

    # Verify account exposure independently from the WS transport check. Any
    # read failure is fail-closed for SHADOW pre-pilot evidence: an unknown
    # account state must never be presented as WOULD_SUBMIT-ready.
    try:
        positions_raw = await client.get_positions()
        positions = [
            p for p in (positions_raw or [])
            if abs(float(p.get("size", 0) or 0)) > 0
        ]
        orders_raw = await client._get(
            "/api/v1/orders", {"status": "active"}, auth=True
        )
        orders = _active_orders(orders_raw)
        unprotected = [
            p for p in positions
            if float(p.get("stopLoss", 0) or 0) <= 0
        ]
        exposure_clear = not positions and not orders
        setattr(client, "_prelive_active_positions", len(positions))
        setattr(client, "_prelive_active_orders", len(orders))
        setattr(client, "_prelive_unprotected_positions", len(unprotected))
        setattr(client, "_prelive_account_exposure_verified", True)
        setattr(client, "_prelive_account_exposure_clear", exposure_clear)
        log.warning(
            "[PRELIVE_ACCOUNT_EXPOSURE] result=%s positions=%s active_orders=%s "
            "unprotected_positions=%s execution_effect=NONE",
            "PASS" if exposure_clear else "BLOCKED",
            len(positions),
            len(orders),
            len(unprotected),
        )
        if orders:
            _log_active_order_forensics(orders, log)
    except Exception as exc:
        exposure_clear = False
        setattr(client, "_prelive_active_positions", None)
        setattr(client, "_prelive_active_orders", None)
        setattr(client, "_prelive_unprotected_positions", None)
        setattr(client, "_prelive_account_exposure_verified", False)
        setattr(client, "_prelive_account_exposure_clear", False)
        log.warning(
            "[PRELIVE_ACCOUNT_EXPOSURE] result=FAIL reason=%s execution_effect=NONE",
            type(exc).__name__,
        )

    try:
        ws_ok = bool(
            await asyncio.wait_for(_private_ws_probe(client, symbol), timeout=20.0)
        )
    except Exception as exc:
        setattr(client, "_prelive_private_ws_probe_ok", False)
        log.warning(
            "[PRIVATE_WS_READONLY_PROBE] result=FAIL symbol=%s reason=%s execution_effect=NONE",
            symbol,
            type(exc).__name__,
        )
        return False

    setattr(client, "_prelive_private_ws_probe_ok", ws_ok)
    log.info(
        "[PRIVATE_WS_READONLY_PROBE] result=%s symbol=%s authenticated=true "
        "subscription_ack=%s execution_effect=NONE",
        "PASS" if ws_ok else "FAIL",
        symbol,
        str(ws_ok).lower(),
    )
    return bool(ws_ok and exposure_clear)
