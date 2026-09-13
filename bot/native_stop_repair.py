"""Repair KuCoin Futures protection with verified conditional closing orders.

KuCoin's official futures SDK uses POST /api/v1/orders for market orders,
including stop/closeOrder parameters. Existing protection is never cancelled.
Only a matching active conditional order read back from the exchange counts
as success; an HTTP acknowledgement alone does not.
"""
import math
import uuid

from bot.conditional_stop_protection import read_stop_orders, _normalized_symbol, _order_active


def _number(value):
    value = float(value or 0)
    if not math.isfinite(value):
        raise ValueError("nonfinite protection input")
    return value


def _matches(order, body):
    try:
        return (
            _order_active(order)
            and _normalized_symbol(order.get("symbol", "")) == _normalized_symbol(body["symbol"])
            and str(order.get("side", "")).lower() == body["side"]
            and order.get("closeOrder") is True
            and str(order.get("stop", "")).lower() == body["stop"]
            and str(order.get("stopPriceType", "")).upper() == body["stopPriceType"]
            and math.isclose(_number(order.get("stopPrice")), float(body["stopPrice"]), rel_tol=1e-12)
        )
    except (ValueError, TypeError, AttributeError):
        return False


async def set_stops(client, symbol, sl, tp, kucoin_mod, log):
    if kucoin_mod.PAPER_TRADE or not kucoin_mod.API_KEY:
        return False
    try:
        sl, tp = _number(sl), _number(tp)
        if sl < 0 or tp < 0 or not (sl > 0 or tp > 0):
            return False
        positions = await client.get_positions()
        pos = next((p for p in positions if p.get("symbol") == symbol and abs(_number(p.get("size"))) > 0), None)
        if pos is None:
            return False
        side = str(pos.get("side", "")).lower()
        if side not in ("buy", "sell", "long", "short"):
            return False
        long = side in ("buy", "long")
        reference = _number(pos.get("markPrice") or pos.get("entryPrice"))
        if reference <= 0:
            return False
        orders = await read_stop_orders(client, symbol)
        if orders is None:
            return False
        for kind, price in (("SL", sl), ("TP", tp)):
            if price <= 0:
                continue
            rounded = client._round_price(price, symbol)
            below = long if kind == "SL" else not long
            if (_number(rounded) >= reference if below else _number(rounded) <= reference):
                log.error("[NATIVE_STOP_REPAIR] symbol=%s kind=%s invalid_trigger_side", symbol, kind)
                return False
            body = dict(symbol=kucoin_mod.to_kucoin(symbol), side="sell" if long else "buy",
                        type="market", stop="down" if below else "up", stopPrice=rounded,
                        stopPriceType="MP", closeOrder=True, reduceOnly=True)
            if not any(_matches(order, body) for order in orders):
                body["clientOid"] = "bgx-stop-" + uuid.uuid4().hex[:30]
                # Preserve this ID across transport retries. On an ambiguous
                # response, the independent stop read below remains authority.
                try:
                    await client._post("/api/v1/orders", body, single_attempt=True)
                except Exception as exc:
                    log.warning("[NATIVE_STOP_REPAIR] symbol=%s post_unconfirmed=%s", symbol, type(exc).__name__)
                orders = await read_stop_orders(client, symbol)
                if orders is None or not any(_matches(order, body) for order in orders):
                    log.error("[NATIVE_STOP_REPAIR] symbol=%s kind=%s readback_unconfirmed", symbol, kind)
                    return False
            log.info("[NATIVE_STOP_REPAIR] symbol=%s kind=%s trigger=%s closeOrder=true confirmed=true existing_stops_preserved=true", symbol, kind, rounded)
        return True
    except Exception as exc:
        log.error("[NATIVE_STOP_REPAIR] symbol=%s failed=%s", symbol, type(exc).__name__)
        return False
