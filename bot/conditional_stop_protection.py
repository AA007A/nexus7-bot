"""Read-only recognition of KuCoin Futures conditional protective stops.

This module never creates, modifies, cancels, or adopts an exchange order.
It only reads currently untriggered stop orders and decides whether they
fully protect an already-open exchange position.

Fail-closed rules:
- inline position stopLoss > 0 remains authoritative;
- otherwise the KuCoin /api/v1/stopOrders read must succeed;
- stop must be active/untriggered, on the same symbol, opposite the position;
- it must be reduce-only or a close-order;
- trigger price must be on the protective side of the position reference price;
- a reduce-only stop must cover the full position size (multiple stops may sum).
"""

import math
from urllib.parse import quote


def _finite_positive(value) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        out = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return out if math.isfinite(out) and out > 0 else 0.0


def inline_stop_confirmed(position: dict) -> bool:
    if not isinstance(position, dict):
        return False
    return _finite_positive(position.get("stopLoss", position.get("stop_loss", 0))) > 0


def _position_direction(position: dict) -> str:
    raw = str(position.get("side", "") or "").strip().lower()
    if raw in {"buy", "long"}:
        return "long"
    if raw in {"sell", "short"}:
        return "short"
    try:
        qty = float(position.get("currentQty", position.get("size", 0)) or 0)
    except (TypeError, ValueError):
        qty = 0.0
    if qty > 0:
        return "long"
    if qty < 0:
        return "short"
    return ""


def _reference_price(position: dict) -> float:
    for key in ("markPrice", "mark_price", "entryPrice", "avgEntryPrice"):
        px = _finite_positive(position.get(key))
        if px > 0:
            return px
    return 0.0


def _order_active(order: dict) -> bool:
    if not isinstance(order, dict):
        return False
    if order.get("stopTriggered") is True:
        return False
    if order.get("isActive") is False:
        return False
    status = str(order.get("status", "") or "").strip().lower()
    if status in {"done", "cancelled", "canceled", "filled", "triggered"}:
        return False
    return True


def _normalized_symbol(raw: str) -> str:
    sym = str(raw or "").upper()
    if sym == "XBTUSDTM":
        return "BTCUSDT"
    if sym.endswith("USDTM"):
        return f"{sym[:-5]}USDT"
    return sym


def _protective_order(order: dict, position: dict, symbol: str) -> tuple[bool, bool, float]:
    """Return (qualifies, closes_full_position, covered_size)."""
    if not _order_active(order):
        return False, False, 0.0
    if _normalized_symbol(order.get("symbol")) != _normalized_symbol(symbol):
        return False, False, 0.0

    direction = _position_direction(position)
    side = str(order.get("side", "") or "").strip().lower()
    if not direction or side not in {"buy", "sell"}:
        return False, False, 0.0
    if direction == "long" and side != "sell":
        return False, False, 0.0
    if direction == "short" and side != "buy":
        return False, False, 0.0

    stop_price = _finite_positive(order.get("stopPrice", order.get("stop_price", 0)))
    reference = _reference_price(position)
    if stop_price <= 0 or reference <= 0:
        return False, False, 0.0
    if direction == "long" and stop_price >= reference:
        return False, False, 0.0
    if direction == "short" and stop_price <= reference:
        return False, False, 0.0

    close_order = order.get("closeOrder") is True
    reduce_only = order.get("reduceOnly") is True
    if not close_order and not reduce_only:
        return False, False, 0.0

    if close_order:
        return True, True, float("inf")

    size = _finite_positive(order.get("size", order.get("qty", 0)))
    if size <= 0:
        return False, False, 0.0
    return True, False, size


async def read_stop_orders(client, symbol: str):
    """Read untriggered KuCoin stop orders. Returns None when unconfirmed."""
    getter = getattr(client, "get_stop_orders", None)
    if callable(getter):
        try:
            data = await getter(symbol)
        except Exception:
            return None
    else:
        raw_get = getattr(client, "_get", None)
        if not callable(raw_get):
            return None
        try:
            kc_symbol = symbol
            try:
                from bot.kucoin import to_kucoin
            except (ImportError, AttributeError):
                to_kucoin = None
            if callable(to_kucoin):
                kc_symbol = to_kucoin(symbol)
            endpoint = f"/api/v1/stopOrders?symbol={quote(str(kc_symbol), safe='')}"
            data = await raw_get(endpoint, auth=True)
        except Exception:
            return None

    if data is None:
        return None
    if isinstance(data, dict):
        items = data.get("items")
        if items is None:
            items = data.get("data") if isinstance(data.get("data"), list) else None
        return list(items) if isinstance(items, list) else None
    if isinstance(data, list):
        return list(data)
    return None


async def conditional_stop_confirmed(client, position: dict) -> tuple[bool, str]:
    """Return (protected, evidence) without mutating the exchange."""
    if inline_stop_confirmed(position):
        return True, "inline_stop"
    if not isinstance(position, dict):
        return False, "invalid_position"

    symbol = str(position.get("symbol", "") or "")
    try:
        position_size = _finite_positive(abs(float(position.get("size", 0) or 0)))
    except (TypeError, ValueError):
        position_size = 0.0
    if not symbol or position_size <= 0:
        return False, "invalid_position"

    orders = await read_stop_orders(client, symbol)
    if orders is None:
        return False, "stop_orders_unconfirmed"

    covered = 0.0
    for order in orders:
        qualifies, full_close, amount = _protective_order(order, position, symbol)
        if not qualifies:
            continue
        if full_close:
            return True, "conditional_close_order"
        covered += amount

    if covered + max(1e-12, position_size * 1e-9) >= position_size:
        return True, "conditional_reduce_only"
    return False, "no_full_protective_stop"
