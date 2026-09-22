"""Repair KuCoin Futures protection with verified conditional closing orders.

The desired SL/TP is still supplied by the existing trading policy.  This
module changes only lifecycle mechanics: BGX-owned protection is deterministic
across retries, a replacement is verified before superseded BGX stops are
retired, and external/user-created orders are never adopted or cancelled.
"""
import asyncio
import math

from bot.conditional_stop_protection import (
    _instrument_info,
    _normalized_symbol,
    _order_active,
    _to_base_size,
    read_stop_orders,
)
from bot import conditional_stop_lifecycle as lifecycle


def _number(value):
    value = float(value or 0)
    if not math.isfinite(value):
        raise ValueError("nonfinite protection input")
    return value


def _same_trigger_price(readback, requested, instrument_info=None):
    try:
        left, right = _number(readback), _number(requested)
        tick = 0.0
        if isinstance(instrument_info, dict):
            tick = _number(instrument_info.get("tickSize", 0))
        tolerance = (
            tick + max(1e-12, abs(right) * 1e-12)
            if tick > 0 else max(1e-12, abs(right) * 1e-12)
        )
        return abs(left - right) <= tolerance
    except (ValueError, TypeError, AttributeError):
        return False


def _matches(order, body, position=None, instrument_info=None):
    try:
        semantic_match = (
            _order_active(order)
            and _normalized_symbol(order.get("symbol", "")) == _normalized_symbol(body["symbol"])
            and str(order.get("side", "")).lower() == body["side"]
            and (order.get("closeOrder") is True or order.get("reduceOnly") is True)
            and str(order.get("stop", "")).lower() == body["stop"]
            and str(order.get("stopPriceType") or body["stopPriceType"]).upper()
            == body["stopPriceType"]
            and _same_trigger_price(order.get("stopPrice"), body["stopPrice"], instrument_info)
        )
        if not semantic_match:
            return False
        if position is None:
            return False
        if order.get("closeOrder") is True:
            return True
        position_qty = abs(_number(position.get("size")))
        if position_qty <= 0:
            return False
        position_base = _to_base_size(
            position_qty, position.get("sizeUnit", "CONTRACTS"), instrument_info
        )
        covered = _to_base_size(
            order.get("size", order.get("qty", 0)),
            order.get("sizeUnit", "CONTRACTS"),
            instrument_info,
        )
        return (
            position_base > 0
            and covered + max(1e-12, position_base * 1e-9) >= position_base
        )
    except (ValueError, TypeError, AttributeError):
        return False


def _existing_bgx_exact(orders, body, position, instrument_info):
    """Return one already-visible BGX-owned exact desired protection order."""
    matches = [
        row for row in (orders or [])
        if lifecycle.is_bgx_owned(row)
        and _matches(row, body, position, instrument_info)
    ]
    if not matches:
        return None
    return max(matches, key=lambda row: float(row.get("updatedAt", row.get("createdAt", 0)) or 0))


async def set_stops(client, symbol, sl, tp, kucoin_mod, log):
    if kucoin_mod.PAPER_TRADE or not kucoin_mod.API_KEY:
        return False
    try:
        sl, tp = _number(sl), _number(tp)
        if sl < 0 or tp < 0 or not (sl > 0 or tp > 0):
            return False
        positions = await client.get_positions()
        pos = next(
            (
                p for p in positions
                if p.get("symbol") == symbol and abs(_number(p.get("size"))) > 0
            ),
            None,
        )
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
        lineage = lifecycle.position_lineage(client, symbol, pos)

        for kind, price in (("SL", sl), ("TP", tp)):
            if price <= 0:
                continue
            rounded = client._round_price(price, symbol)
            below = long if kind == "SL" else not long
            trigger = _number(rounded)
            if kind == "SL":
                invalid = (long and trigger >= reference) or ((not long) and trigger <= reference)
            else:
                invalid = (long and trigger <= reference) or ((not long) and trigger >= reference)
            if invalid:
                log.error(
                    "[NATIVE_STOP_REPAIR] symbol=%s kind=%s invalid_trigger_side",
                    symbol, kind,
                )
                return False

            body = dict(
                symbol=kucoin_mod.to_kucoin(symbol),
                side="sell" if long else "buy",
                type="market",
                stop="down" if below else "up",
                stopPrice=rounded,
                stopPriceType="MP",
                closeOrder=True,
                reduceOnly=True,
            )

            # A visible BGX-owned exact stop is already authoritative exchange
            # truth. It can become canonical without creating another order.
            exact = _existing_bgx_exact(orders, body, pos, instrument_info)
            if exact is not None:
                canonical_oid = str(exact.get("clientOid") or "")
                cleanup_ok, superseded, external = await lifecycle.cleanup_superseded(
                    client, symbol, side, kind, canonical_oid, read_stop_orders
                )
                log.info(
                    "[PROTECTION_READBACK] symbol=%s kind=%s canonical_client_oid=%s "
                    "superseded_bgx=%s external=%s desired_trigger=%s "
                    "verified_trigger=%s cleanup_status=%s",
                    symbol, kind, canonical_oid, superseded, external, rounded,
                    exact.get("stopPrice"), "VERIFIED" if cleanup_ok else "UNCONFIRMED",
                )
                if not cleanup_ok:
                    return False
                orders = await read_stop_orders(client, symbol)
                if orders is None:
                    return False
                continue

            candidate = await lifecycle.prepare_candidate(
                client,
                symbol,
                side,
                body["side"],
                kind,
                lineage,
                str(rounded),
            )
            body["clientOid"] = candidate["client_oid"]

            if not candidate.get("post_allowed"):
                log.error(
                    "[NATIVE_STOP_REPAIR] symbol=%s kind=%s readback_unconfirmed "
                    "candidate=%s reason=%s resubmit=false",
                    symbol, kind, body["clientOid"], candidate.get("reason"),
                )
                return False

            result = None
            try:
                result = await client._post("/api/v1/orders", body, single_attempt=True)
            except Exception as exc:
                if "300004" in str(exc):
                    await lifecycle.mark_capacity_blocked(
                        client, candidate["slot_key"], len(orders)
                    )
                    log.error(
                        "[NATIVE_STOP_CAPACITY] symbol=%s code=300004 stops=%s "
                        "action=RECONCILE_NO_RESUBMIT",
                        symbol, len(orders),
                    )
                else:
                    log.warning(
                        "[NATIVE_STOP_REPAIR] symbol=%s post_unconfirmed=%s",
                        symbol, type(exc).__name__,
                    )

            confirmed = None
            for attempt in range(4):
                if attempt:
                    await asyncio.sleep(0.25 * attempt)
                orders = await read_stop_orders(client, symbol)
                if orders is None:
                    continue
                confirmed = next(
                    (
                        row for row in orders
                        if str(row.get("clientOid") or "") == body["clientOid"]
                        and _matches(row, body, pos, instrument_info)
                    ),
                    None,
                )
                if confirmed is not None:
                    break

            if confirmed is None:
                # KuCoin's client currently returns {} for permanent 300004.
                # A 50-item authoritative stop inventory therefore provides a
                # bounded capacity signal even when the transport swallowed the
                # numeric code after logging it.
                if isinstance(orders, list) and len(orders) >= 50 and not result:
                    await lifecycle.mark_capacity_blocked(
                        client, candidate["slot_key"], len(orders)
                    )
                    log.error(
                        "[NATIVE_STOP_CAPACITY] symbol=%s code=300004 stops=%s "
                        "action=RECONCILE_NO_RESUBMIT",
                        symbol, len(orders),
                    )
                log.error(
                    "[NATIVE_STOP_REPAIR] symbol=%s kind=%s readback_unconfirmed "
                    "candidate=%s resubmit=false",
                    symbol, kind, body["clientOid"],
                )
                return False

            await lifecycle.mark_verified(
                client,
                candidate["slot_key"],
                str(confirmed.get("id") or confirmed.get("orderId") or ""),
            )
            cleanup_ok, superseded, external = await lifecycle.cleanup_superseded(
                client, symbol, side, kind, body["clientOid"], read_stop_orders
            )
            log.info(
                "[PROTECTION_READBACK] symbol=%s kind=%s canonical_client_oid=%s "
                "superseded_bgx=%s external=%s desired_trigger=%s "
                "verified_trigger=%s cleanup_status=%s",
                symbol, kind, body["clientOid"], superseded, external, rounded,
                confirmed.get("stopPrice"), "VERIFIED" if cleanup_ok else "UNCONFIRMED",
            )
            if not cleanup_ok:
                return False
            orders = await read_stop_orders(client, symbol)
            if orders is None:
                return False

            log.info(
                "[NATIVE_STOP_REPAIR] symbol=%s kind=%s trigger=%s closeOrder=true "
                "confirmed=true canonical_client_oid=%s superseded_cleanup=verified",
                symbol, kind, rounded, body["clientOid"],
            )
        return True
    except Exception as exc:
        log.error(
            "[NATIVE_STOP_REPAIR] symbol=%s failed=%s",
            symbol, type(exc).__name__,
        )
        return False
