"""Fail-closed, read-only gates immediately before an order dispatch.

This module never sends, cancels or modifies an exchange order. It is designed
for the final pre-dispatch recheck so a stale analysis cannot proceed after
account exposure or market microstructure changes.

SHADOW and LIVE consume the same microstructure evaluator. LIVE additionally
uses ``live_microstructure_recheck`` to perform fresh REST ticker/order-book
reads immediately before the engine's durable dispatch boundary.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import math
import os
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


def limits_from_env() -> MicrostructureLimits:
    """Single configuration contract for SHADOW/LIVE execution quality."""
    return MicrostructureLimits(
        max_spread_bps=float(os.environ.get("NEXUS_MAX_SPREAD_BPS", "12")),
        max_signal_drift_bps=float(os.environ.get("NEXUS_MAX_SIGNAL_DRIFT_BPS", "20")),
        min_depth_multiple=float(os.environ.get("NEXUS_MIN_DEPTH_MULTIPLE", "3")),
    )


def _num(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def normalize_orderbook_base_units(
    instruments: Mapping[str, Mapping[str, Any]],
    symbol: str,
    raw: Mapping[str, Any] | None,
) -> dict | None:
    """Convert KuCoin Futures contract depth to base-asset quantities.

    ``evaluate_microstructure`` compares visible depth with the base quantity
    submitted by the engine, so the exchange's contract counts must never be
    compared directly with base units.
    """
    if not isinstance(raw, Mapping):
        return None
    info = instruments.get(symbol, {}) if isinstance(instruments, Mapping) else {}
    multiplier = _num((info or {}).get("multiplier"))
    if multiplier <= 0:
        return None

    def _levels(*keys: str) -> list[list[float]]:
        rows = None
        for key in keys:
            candidate = raw.get(key)
            if candidate is not None:
                rows = candidate
                break
        if not isinstance(rows, Iterable) or isinstance(rows, (str, bytes, Mapping)):
            return []
        out: list[list[float]] = []
        for row in rows:
            if isinstance(row, Mapping):
                price = _num(row.get("price"))
                contracts = _num(row.get("size") or row.get("qty") or row.get("quantity"))
            elif isinstance(row, (list, tuple)) and len(row) >= 2:
                price = _num(row[0])
                contracts = _num(row[1])
            else:
                continue
            if price > 0 and contracts >= 0:
                out.append([price, contracts * multiplier])
        return out

    return {
        "bids": _levels("b", "bids"),
        "asks": _levels("a", "asks"),
    }


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


async def live_microstructure_recheck(
    client,
    *,
    instruments: Mapping[str, Mapping[str, Any]],
    symbol: str,
    signal_entry: float,
    side: str,
    qty: float,
    limits: MicrostructureLimits | None = None,
    timeout_s: float = 4.0,
) -> PreDispatchResult:
    """Fresh REST spread/depth/drift check for the LIVE dispatch boundary.

    Cached quotes are deliberately not used here: this check exists precisely
    to catch market movement that happened after signal analysis. Any timeout,
    exchange-read error, malformed book, or missing top-of-book blocks the new
    opening order. Reduce-only emergency/exit orders are outside this function.
    """
    try:
        ticker, raw_book = await asyncio.wait_for(
            asyncio.gather(
                client.get_ticker(symbol),
                client.get_orderbook(symbol, depth=20),
            ),
            timeout=float(timeout_s),
        )
    except asyncio.TimeoutError:
        return PreDispatchResult(False, ["MICROSTRUCTURE_RECHECK_TIMEOUT"], {})
    except Exception:
        return PreDispatchResult(False, ["MICROSTRUCTURE_RECHECK_FAILED"], {})

    book = normalize_orderbook_base_units(instruments, symbol, raw_book)
    return evaluate_microstructure(
        signal_entry=float(signal_entry),
        side=side,
        qty=float(qty),
        ticker=ticker if isinstance(ticker, Mapping) else {},
        orderbook=book,
        limits=limits or limits_from_env(),
    )


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
