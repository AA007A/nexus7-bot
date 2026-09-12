"""Read-only proof of BGX position ownership after a process restart.

A live exchange position is managed again only when one unique durable BGX
FILLED order explains the current exposure and the exchange independently
confirms the same order identity/fill. Symbol/side/size similarity alone is
never sufficient. Any missing, ambiguous or conflicting evidence fails closed
and leaves the position EXTERNAL/read-only.

This module contains no exchange mutation.
"""
from __future__ import annotations

from dataclasses import dataclass
import math

from bot.conditional_stop_protection import conditional_stop_confirmed
from bot.quantity import contracts_to_base, quantity_rules


@dataclass(frozen=True)
class OwnershipProof:
    recovered: bool
    reason: str
    symbol: str = ""
    client_oid: str = ""
    order_id: str = ""
    side: str = ""
    base_qty: float = 0.0
    protection: str = ""


def _normalized_symbol(raw: str) -> str:
    sym = str(raw or "").upper()
    if sym == "XBTUSDTM":
        return "BTCUSDT"
    if sym.endswith("USDTM"):
        return f"{sym[:-5]}USDT"
    return sym


def _normalized_side(raw: str) -> str:
    side = str(raw or "").strip().lower()
    if side in {"buy", "long"}:
        return "Buy"
    if side in {"sell", "short"}:
        return "Sell"
    return ""


def _instrument_info(client, symbol: str):
    standard = _normalized_symbol(symbol)
    getter = getattr(client, "get_instruments", None)
    if callable(getter):
        try:
            instruments = getter() or {}
        except Exception:
            instruments = {}
        if isinstance(instruments, dict):
            info = instruments.get(standard)
            if isinstance(info, dict):
                return info
    instruments = getattr(client, "_instruments", None)
    if isinstance(instruments, dict):
        info = instruments.get(standard)
        if isinstance(info, dict):
            return info
    return None


def _base_lot(info) -> float:
    try:
        multiplier, lot, _, _ = quantity_rules(info)
        return float(multiplier * lot)
    except (KeyError, TypeError, ValueError):
        return 0.0


def _same_base_qty(left: float, right: float, info) -> bool:
    try:
        a = float(left)
        b = float(right)
    except (TypeError, ValueError):
        return False
    if not (math.isfinite(a) and math.isfinite(b)) or a <= 0 or b <= 0:
        return False
    lot = _base_lot(info)
    if lot <= 0:
        return False
    tolerance = max(1e-12, lot * 1e-9)
    return abs(a - b) <= tolerance


def _position_base_qty(position: dict, info) -> float:
    if not isinstance(position, dict):
        return 0.0
    try:
        raw = abs(float(position.get("size", 0) or 0))
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(raw) or raw <= 0:
        return 0.0
    if str(position.get("sizeUnit", "") or "").upper() == "BASE_ASSET":
        return raw
    try:
        return float(contracts_to_base(raw, info))
    except (KeyError, TypeError, ValueError):
        return 0.0


def _filled_base_qty(status: dict, info) -> float:
    if not isinstance(status, dict):
        return 0.0
    raw = status.get("filledSize", status.get("dealSize", 0))
    try:
        return float(contracts_to_base(raw, info))
    except (KeyError, TypeError, ValueError):
        return 0.0


def _reject(reason: str, *, symbol: str = "") -> OwnershipProof:
    return OwnershipProof(False, reason, symbol=symbol)


async def prove_restart_ownership(engine, position: dict) -> OwnershipProof:
    """Prove one startup position belongs to BGX using only read-only evidence."""
    if not isinstance(position, dict):
        return _reject("invalid_position")

    symbol = _normalized_symbol(position.get("symbol"))
    side = _normalized_side(position.get("side"))
    if not symbol or not side:
        return _reject("invalid_position_identity", symbol=symbol)

    info = _instrument_info(getattr(engine, "client", None), symbol)
    if not isinstance(info, dict) or _base_lot(info) <= 0:
        return _reject("instrument_metadata_unconfirmed", symbol=symbol)

    position_qty = _position_base_qty(position, info)
    if position_qty <= 0:
        return _reject("position_quantity_unconfirmed", symbol=symbol)

    registry = getattr(engine, "orders", None)
    snapshot_fn = getattr(registry, "snapshot", None)
    if not callable(snapshot_fn):
        return _reject("durable_registry_unavailable", symbol=symbol)
    try:
        records = snapshot_fn() or []
    except Exception:
        return _reject("durable_registry_unreadable", symbol=symbol)

    candidates = []
    for record in records:
        if not isinstance(record, dict):
            continue
        client_oid = str(record.get("client_oid") or "")
        order_id = str(record.get("order_id") or "")
        if not client_oid.startswith("bgx7-") or not order_id:
            continue
        if str(record.get("state") or "") != "FILLED":
            continue
        if _normalized_symbol(record.get("symbol")) != symbol:
            continue
        if _normalized_side(record.get("side")) != side:
            continue
        try:
            durable_qty = float(record.get("qty", 0) or 0)
            durable_filled = float(record.get("filled_qty", 0) or 0)
        except (TypeError, ValueError):
            continue
        if not _same_base_qty(durable_qty, position_qty, info):
            continue
        if durable_filled > 0 and not _same_base_qty(durable_filled, position_qty, info):
            continue
        candidates.append(record)

    if not candidates:
        return _reject("no_exact_durable_fill", symbol=symbol)
    if len(candidates) != 1:
        return _reject("ambiguous_durable_fills", symbol=symbol)

    candidate = candidates[0]
    client_oid = str(candidate["client_oid"])
    order_id = str(candidate["order_id"])
    getter = getattr(getattr(engine, "client", None), "get_order_status", None)
    if not callable(getter):
        return _reject("exchange_order_reader_unavailable", symbol=symbol)
    try:
        status = await getter(order_id)
    except Exception:
        return _reject("exchange_order_read_failed", symbol=symbol)
    if not isinstance(status, dict) or status.get("_unknown") or status.get("_synthetic"):
        return _reject("exchange_order_unconfirmed", symbol=symbol)

    status_order_id = str(status.get("orderId") or status.get("id") or "")
    status_client_oid = str(status.get("clientOid") or "")
    if status_order_id != order_id:
        return _reject("exchange_order_id_mismatch", symbol=symbol)
    if status_client_oid != client_oid:
        return _reject("exchange_client_oid_mismatch", symbol=symbol)
    if _normalized_symbol(status.get("symbol")) != symbol:
        return _reject("exchange_symbol_mismatch", symbol=symbol)
    if _normalized_side(status.get("side")) != side:
        return _reject("exchange_side_mismatch", symbol=symbol)
    if status.get("isActive") is not False:
        return _reject("exchange_order_not_terminal", symbol=symbol)
    if status.get("cancelExist") is True:
        return _reject("exchange_order_cancelled", symbol=symbol)

    filled_qty = _filled_base_qty(status, info)
    if not _same_base_qty(filled_qty, position_qty, info):
        return _reject("exchange_fill_quantity_mismatch", symbol=symbol)

    protected, protection = await conditional_stop_confirmed(engine.client, position)
    if not protected:
        return _reject("protection_unconfirmed", symbol=symbol)

    return OwnershipProof(
        True,
        "exact_durable_exchange_proof",
        symbol=symbol,
        client_oid=client_oid,
        order_id=order_id,
        side=side,
        base_qty=position_qty,
        protection=protection,
    )
