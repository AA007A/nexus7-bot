"""Fail-closed, read-only gates immediately before an order dispatch.

This module never sends, cancels or modifies an exchange order. It is designed
for the final pre-dispatch recheck so a stale analysis cannot proceed after
account exposure or market microstructure changes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any, Iterable, Mapping


@dataclass(frozen=True)
class MicrostructureLimits:
    max_spread_bps: float = 12.0
    max_signal_drift_bps: float = 20.0
    min_depth_multiple: float = 3.0


@dataclass
class PreDispatchResult:
    allowed: bool
    blockers: list[str] = field(default_factory=list)
    metrics: dict[str, float] = field(default_factory=dict)


def _num(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def evaluate_microstructure(
    *,
    signal_entry: float,
    side: str,
    qty: float,
    ticker: Mapping[str, Any],
    orderbook: Mapping[str, Any] | None,
    limits: MicrostructureLimits = MicrostructureLimits(),
) -> PreDispatchResult:
    blockers: list[str] = []
    metrics: dict[str, float] = {}
    if signal_entry <= 0 or qty <= 0:
        return PreDispatchResult(False, ["INVALID_SIGNAL_OR_QTY"], metrics)

    bid = _num(ticker.get("bestBid") or ticker.get("bestBidPrice") or ticker.get("bid"))
    ask = _num(ticker.get("bestAsk") or ticker.get("bestAskPrice") or ticker.get("ask"))
    last = _num(ticker.get("lastPrice") or ticker.get("price"))
    if bid <= 0 or ask <= 0 or ask < bid:
        return PreDispatchResult(False, ["TOP_OF_BOOK_UNAVAILABLE"], metrics)

    mid = (bid + ask) / 2.0
    spread_bps = (ask - bid) / mid * 10000.0
    executable = ask if str(side).upper() in ("BUY", "LONG") else bid
    drift_bps = abs(executable - signal_entry) / signal_entry * 10000.0
    metrics.update(spread_bps=spread_bps, signal_drift_bps=drift_bps, executable_price=executable)

    if spread_bps > limits.max_spread_bps:
        blockers.append("SPREAD_TOO_WIDE")
    if drift_bps > limits.max_signal_drift_bps:
        blockers.append("SIGNAL_PRICE_STALE")

    # Depth is optional in market analysis but mandatory for this execution gate.
    if not isinstance(orderbook, Mapping):
        blockers.append("ORDERBOOK_UNAVAILABLE")
        return PreDispatchResult(not blockers, blockers, metrics)

    levels = orderbook.get("asks") if str(side).upper() in ("BUY", "LONG") else orderbook.get("bids")
    if not isinstance(levels, Iterable):
        blockers.append("ORDERBOOK_UNAVAILABLE")
        return PreDispatchResult(not blockers, blockers, metrics)

    visible_base = 0.0
    for level in levels:
        if isinstance(level, Mapping):
            size = _num(level.get("size") or level.get("qty") or level.get("quantity"))
        elif isinstance(level, (list, tuple)) and len(level) >= 2:
            size = _num(level[1])
        else:
            size = 0.0
        if size > 0:
            visible_base += size

    depth_multiple = visible_base / qty if qty > 0 else 0.0
    metrics["depth_multiple"] = depth_multiple
    if depth_multiple < limits.min_depth_multiple:
        blockers.append("INSUFFICIENT_BOOK_DEPTH")

    if last > 0:
        metrics["last_to_executable_bps"] = abs(executable - last) / last * 10000.0
    return PreDispatchResult(not blockers, blockers, metrics)


async def recheck_exchange_exposure(client, symbol: str) -> PreDispatchResult:
    """Read positions + active orders immediately before dispatch.

    Any read failure blocks. Existing exposure in the candidate symbol blocks.
    Any active order blocks because it consumes collateral and may become a
    position asynchronously after this check.
    """
    blockers: list[str] = []
    metrics: dict[str, float] = {}
    try:
        positions = await client.get_positions()
    except Exception:
        return PreDispatchResult(False, ["POSITION_RECHECK_FAILED"], metrics)

    try:
        active = await client._get("/api/v1/orders", {"status": "active"}, auth=True)
    except Exception:
        return PreDispatchResult(False, ["ACTIVE_ORDER_RECHECK_FAILED"], metrics)

    pos_count = 0
    symbol_exposure = 0
    for p in positions or []:
        size = abs(_num((p or {}).get("size"))) if isinstance(p, Mapping) else 0.0
        if size <= 0:
            continue
        pos_count += 1
        if str((p or {}).get("symbol", "")) == symbol:
            symbol_exposure += 1

    if isinstance(active, Mapping):
        orders = active.get("items") or active.get("data") or active.get("orders") or []
    elif isinstance(active, list):
        orders = active
    else:
        orders = []
    active_count = sum(1 for o in orders if isinstance(o, Mapping))

    metrics.update(active_positions=float(pos_count), active_orders=float(active_count))
    if symbol_exposure:
        blockers.append("SYMBOL_ALREADY_EXPOSED")
    if active_count:
        blockers.append("ACTIVE_EXCHANGE_ORDER_PRESENT")

    return PreDispatchResult(not blockers, blockers, metrics)
