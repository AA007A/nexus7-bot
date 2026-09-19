"""Repair KuCoin Futures protection with verified conditional closing orders.

KuCoin's official futures SDK uses POST /api/v1/orders for market orders,
including stop/closeOrder parameters. Existing protection is never cancelled.
Only a matching active conditional order read back from the exchange counts
as success; an HTTP acknowledgement alone does not.
"""
import asyncio
import math
import uuid

from bot.conditional_stop_protection import (read_stop_orders, _normalized_symbol, _order_active,
    _instrument_info, _protective_order)


def _number(value):
    value = float(value or 0)
    if not math.isfinite(value):
        raise ValueError("nonfinite protection input")
    return value


def _matches(order, body, position=None, instrument_info=None):
    try:
        semantic_match = (
            _order_active(order)
            and _normalized_symbol(order.get("symbol", "")) == _normalized_symbol(body["symbol"])
            and str(order.get("side", "")).lower() == body["side"]
            and (order.get("closeOrder") is True or order.get("reduceOnly") is True)
            and str(order.get("stop", "")).lower() == body["stop"]
            and str(order.get("stopPriceType", "")).upper() == body["stopPriceType"]
            and math.isclose(_number(order.get("stopPrice")), float(body["stopPrice"]), rel_tol=1e-12)
        )
        if not semantic_match:
            return False
        if position is None:
            return False
        qualifies, full_close, covered = _protective_order(
            order, position, body["symbol"], instrument_info
        )
        if not qualifies:
            return False
        if full_close:
            return True
        # A reduceOnly representation is equivalent only when this exact stop
        # independently covers the full remaining position. Do not aggregate
        # unrelated stops for readback confirmation.
        position_qty = abs(_number(position.get("size")))
        if position_qty <= 0:
            return False
        from bot.conditional_stop_protection import _to_base_size
        position_base = _to_base_size(
            position_qty, position.get("sizeUnit", "CONTRACTS"), instrument_info
        )
        return position_base > 0 and covered + max(1e-12, position_base * 1e-9) >= position_base
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
        instrument_info = _instrument_info(client, symbol)
        if orders is None:
            return False
        for kind, price in (("SL", sl), ("TP", tp)):
            if price <= 0:
                continue
            rounded = client._round_price(price, symbol)
            below = long if kind == "SL" else not long
            # A break-even SL is intentionally allowed at the position entry.
            # Validate protective side against entry for ordinary stops, while
            # permitting equality at entry; mark price may already be beyond BE.
            entry = _number(pos.get("entryPrice") or reference)
            trigger = _number(rounded)
            if kind == "SL":
                # Validate against current mark so BE and profitable trailing
                # stops are both valid while already-crossed stops fail closed.
                invalid = (long and trigger >= reference) or ((not long) and trigger <= reference)
            else:
                invalid = (long and trigger <= reference) or ((not long) and trigger >= reference)
            if invalid:
                log.error("[NATIVE_STOP_REPAIR] symbol=%s kind=%s invalid_trigger_side", symbol, kind)
                return False
            body = dict(symbol=kucoin_mod.to_kucoin(symbol), side="sell" if long else "buy",
                        type="market", stop="down" if below else "up", stopPrice=rounded,
                        stopPriceType="MP", closeOrder=True, reduceOnly=True)
            if not any(_matches(order, body, pos, instrument_info) for order in orders):
                body["clientOid"] = "bgx-stop-" + uuid.uuid4().hex[:30]
                # Preserve this ID across transport retries. On an ambiguous
                # response, the independent stop read below remains authority.
                try:
                    await client._post("/api/v1/orders", body, single_attempt=True)
                except Exception as exc:
                    log.warning("[NATIVE_STOP_REPAIR] symbol=%s post_unconfirmed=%s", symbol, type(exc).__name__)
                confirmed = False
                for attempt in range(4):
                    if attempt:
                        await asyncio.sleep(0.25 * attempt)
                    orders = await read_stop_orders(client, symbol)
                    if orders is not None and any(_matches(order, body) for order in orders):
                        confirmed = True
                        break
                if not confirmed:
                    log.error("[NATIVE_STOP_REPAIR] symbol=%s kind=%s readback_unconfirmed", symbol, kind)
                    return False
            log.info("[NATIVE_STOP_REPAIR] symbol=%s kind=%s trigger=%s closeOrder=true confirmed=true existing_stops_preserved=true", symbol, kind, rounded)
        return True
    except Exception as exc:
        log.error("[NATIVE_STOP_REPAIR] symbol=%s failed=%s", symbol, type(exc).__name__)
        return False
