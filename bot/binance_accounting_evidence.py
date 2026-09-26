"""Read-only Binance USD-M accounting evidence for NEXUS-7.

This adapter deliberately has NO trading or daily-PnL authority. It collects
bounded Binance USER_DATA evidence and classifies order ownership only when the
exchange order identity is explicit. It never submits/cancels orders, changes
leverage, changes sizing, or authorizes entries.

A Binance user trade is a fill, not a closed-position record. Therefore this
module MUST NOT synthesize a KuCoin-style closed position or mark BGX_CONFIRMED
until a separate lifecycle reconstruction proves the complete opening/closing
fill set and durable opening-order lineage.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time

from bot import database as db
from bot.logger import log

ACCOUNTING_SOURCE = "BINANCE_USDM_OBSERVATION"
ACCOUNTING_AUTHORITY = False

_TRADE_FIELDS = (
    "symbol", "id", "orderId", "side", "price", "qty", "quoteQty",
    "realizedPnl", "commission", "commissionAsset", "time", "buyer",
    "maker", "positionSide",
)
_INCOME_FIELDS = (
    "symbol", "incomeType", "income", "asset", "info", "time",
    "tranId", "tradeId",
)
_ORDER_FIELDS = (
    "symbol", "orderId", "clientOrderId", "status", "side", "positionSide",
    "type", "origType", "reduceOnly", "closePosition", "executedQty",
    "avgPrice", "time", "updateTime",
)


def _bounded_window(start_ms: int, end_ms: int, max_ms: int) -> tuple[int, int]:
    start_ms = int(start_ms)
    end_ms = int(end_ms)
    if start_ms <= 0 or end_ms <= start_ms or end_ms - start_ms > max_ms:
        raise ValueError("invalid accounting window")
    return start_ms, end_ms


def _safe_row(row: dict, fields: tuple[str, ...]) -> dict:
    if not isinstance(row, dict):
        raise ValueError("accounting row is not an object")
    return {key: row[key] for key in fields if key in row}


async def collect_user_trades(client, symbol: str, start_ms: int, end_ms: int) -> list[dict]:
    """Collect a complete <=7d trade window for one symbol or fail closed."""
    start_ms, end_ms = _bounded_window(start_ms, end_ms, 7 * 86400000)
    symbol = str(symbol or "").upper()
    if not symbol:
        raise ValueError("symbol required")

    data = await client._get(
        "/fapi/v1/userTrades",
        params={
            "symbol": symbol,
            "startTime": start_ms,
            "endTime": end_ms,
            "limit": 1000,
        },
        auth=True,
    )
    if not isinstance(data, list):
        raise ValueError("userTrades response unconfirmed")
    if len(data) >= 1000:
        # A full page does not prove complete coverage. Refuse to invent it.
        raise ValueError("userTrades coverage exceeds single-window budget")

    rows: dict[str, dict] = {}
    for raw in data:
        if not isinstance(raw, dict):
            raise ValueError("invalid userTrades row")
        if str(raw.get("symbol") or "").upper() != symbol:
            raise ValueError("userTrades symbol mismatch")
        if raw.get("id") is None or raw.get("orderId") is None:
            raise ValueError("userTrades identity missing")
        ts = int(raw.get("time", 0) or 0)
        if not start_ms <= ts <= end_ms:
            raise ValueError("userTrades row outside requested window")
        safe = _safe_row(raw, _TRADE_FIELDS)
        token = str(raw["id"])
        if token in rows and rows[token] != safe:
            raise ValueError("conflicting duplicate user trade")
        rows[token] = safe
    return sorted(rows.values(), key=lambda row: (int(row.get("time", 0)), int(row["id"])))


async def collect_orders(client, symbol: str, start_ms: int, end_ms: int) -> list[dict]:
    """Collect Binance normal-order identity evidence for a <=7d symbol window."""
    start_ms, end_ms = _bounded_window(start_ms, end_ms, 7 * 86400000)
    symbol = str(symbol or "").upper()
    if not symbol:
        raise ValueError("symbol required")

    data = await client._get(
        "/fapi/v1/allOrders",
        params={
            "symbol": symbol,
            "startTime": start_ms,
            "endTime": end_ms,
            "limit": 1000,
        },
        auth=True,
    )
    if not isinstance(data, list):
        raise ValueError("allOrders response unconfirmed")
    if len(data) >= 1000:
        raise ValueError("allOrders coverage exceeds single-window budget")

    rows: dict[str, dict] = {}
    for raw in data:
        if not isinstance(raw, dict):
            raise ValueError("invalid allOrders row")
        if str(raw.get("symbol") or "").upper() != symbol:
            raise ValueError("allOrders symbol mismatch")
        if raw.get("orderId") is None:
            raise ValueError("allOrders identity missing")
        safe = _safe_row(raw, _ORDER_FIELDS)
        token = str(raw["orderId"])
        if token in rows and rows[token] != safe:
            raise ValueError("conflicting duplicate order")
        rows[token] = safe
    return list(rows.values())


async def collect_income(client, start_ms: int, end_ms: int) -> list[dict]:
    """Collect paginated Binance futures income evidence for a <=7d window."""
    start_ms, end_ms = _bounded_window(start_ms, end_ms, 7 * 86400000)
    rows: dict[str, dict] = {}
    for page in range(1, 21):
        data = await client._get(
            "/fapi/v1/income",
            params={
                "startTime": start_ms,
                "endTime": end_ms,
                "page": page,
                "limit": 1000,
            },
            auth=True,
        )
        if not isinstance(data, list):
            raise ValueError("income response unconfirmed")
        for raw in data:
            if not isinstance(raw, dict):
                raise ValueError("invalid income row")
            if raw.get("incomeType") is None or raw.get("tranId") is None:
                raise ValueError("income identity missing")
            ts = int(raw.get("time", 0) or 0)
            if not start_ms <= ts <= end_ms:
                raise ValueError("income row outside requested window")
            safe = _safe_row(raw, _INCOME_FIELDS)
            token = f"{raw.get('incomeType')}:{raw.get('tranId')}:{raw.get('tradeId', '')}"
            if token in rows and rows[token] != safe:
                raise ValueError("conflicting duplicate income row")
            rows[token] = safe
        if len(data) < 1000:
            return sorted(
                rows.values(),
                key=lambda row: (
                    int(row.get("time", 0)),
                    str(row.get("incomeType", "")),
                    str(row.get("tranId", "")),
                ),
            )
    raise ValueError("income coverage exceeds pagination budget")


def _registry_order_ids(registry) -> set[str]:
    if not isinstance(registry, list):
        return set()
    return {
        str(row.get("order_id"))
        for row in registry
        if isinstance(row, dict) and row.get("order_id") is not None
    }


def classify_trade_origin(trade: dict, order: dict | None, registry) -> tuple[str, str]:
    """Classify exchange order ownership without claiming a full position lifecycle."""
    if not isinstance(trade, dict) or trade.get("orderId") is None:
        return "UNKNOWN_UNATTRIBUTED", "TRADE_ORDER_ID_MISSING"
    if not isinstance(order, dict):
        return "UNKNOWN_UNATTRIBUTED", "ORDER_IDENTITY_UNAVAILABLE"

    order_id = str(trade["orderId"])
    if str(order.get("orderId") or "") != order_id:
        return "UNKNOWN_UNATTRIBUTED", "ORDER_IDENTITY_MISMATCH"
    if str(order.get("symbol") or "").upper() != str(trade.get("symbol") or "").upper():
        return "UNKNOWN_UNATTRIBUTED", "ORDER_SYMBOL_MISMATCH"

    client_oid = order.get("clientOrderId")
    if not isinstance(client_oid, str) or not client_oid:
        return "UNKNOWN_UNATTRIBUTED", "CLIENT_ORDER_ID_MISSING"

    durable_ids = _registry_order_ids(registry)
    if client_oid.startswith("bgx7-"):
        if order_id in durable_ids:
            # Strong order-level evidence, deliberately not BGX_CONFIRMED:
            # a fill is not by itself a complete position lifecycle.
            return "BGX_ORDER_EVIDENCE", "BGX_CLIENT_ORDER_ID_AND_DURABLE_ORDER"
        return "UNKNOWN_UNATTRIBUTED", "BGX_CLIENT_ORDER_ID_WITHOUT_DURABLE_ORDER"

    if order_id in durable_ids:
        return "UNKNOWN_UNATTRIBUTED", "DURABLE_ORDER_CONFLICTS_WITH_NON_BGX_CLIENT_ID"
    return "MANUAL_EXTERNAL", "NON_BGX_CLIENT_ORDER_ID_CONFIRMED"


def _opening_fill_order_ids(receipt, row):
    """Compatibility helper for external-origin telemetry; never grants authority."""
    fills = receipt.get("fills") if isinstance(receipt, dict) else None
    if not isinstance(fills, list):
        return []
    wanted = {
        "LONG": "BUY",
        "SHORT": "SELL",
        "BUY": "BUY",
        "SELL": "SELL",
    }.get(str((row or {}).get("side") or "").upper())
    if not wanted:
        return []
    seen = set()
    result = []
    for fill in fills:
        if not isinstance(fill, dict) or str(fill.get("side") or "").upper() != wanted:
            continue
        order_id = str(fill.get("orderId") or "")
        if order_id and order_id not in seen:
            seen.add(order_id)
            result.append(order_id)
    return result


async def _classify_origin(client, receipt, registry, row):
    """Compatibility contract; full BGX lifecycle authority remains intentionally disabled."""
    if isinstance(receipt, dict) and receipt.get("origin_class") == "MANUAL_EXTERNAL":
        return "MANUAL_EXTERNAL", str(receipt.get("origin_reason") or "NON_BGX_ORDER")
    return "UNKNOWN_UNATTRIBUTED", "BINANCE_POSITION_LIFECYCLE_NOT_YET_RECONSTRUCTED"


async def _load_registry() -> list[dict]:
    from bot.durable_execution import _ORDER_KEY
    raw = await db.load_key_value(_ORDER_KEY, strict=True)
    if not raw:
        return []
    value = json.loads(raw)
    if not isinstance(value, dict) or value.get("version") != 1:
        raise ValueError("invalid durable order registry")
    orders = value.get("orders")
    if not isinstance(orders, list):
        raise ValueError("invalid durable order registry orders")
    return orders


def _normalize_registry_symbol(raw: str, instruments: set[str]) -> str | None:
    symbol = str(raw or "").upper()
    if symbol in instruments:
        return symbol
    if symbol.endswith("M") and symbol[:-1] in instruments:
        return symbol[:-1]
    return None


async def audit(engine):
    """Persist recent trade/order ownership evidence; authority remains false."""
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - 48 * 3600000
    try:
        registry = await _load_registry()
        instruments = set(getattr(engine.client, "_instruments", {}) or {})
        symbols = {
            normalized
            for row in registry
            if isinstance(row, dict)
            for normalized in [_normalize_registry_symbol(row.get("symbol", ""), instruments)]
            if normalized
        }
        # Keep the observation path testable even before a Binance durable order
        # exists, while bounding shared-egress API weight to one symbol.
        if not symbols and "BTCUSDT" in instruments:
            symbols = {"BTCUSDT"}

        counts = {
            "trades": 0,
            "bgx_order_evidence": 0,
            "manual_external": 0,
            "unknown": 0,
        }
        for symbol in sorted(symbols)[:20]:
            trades, orders = await asyncio.gather(
                collect_user_trades(engine.client, symbol, start_ms, end_ms),
                collect_orders(engine.client, symbol, start_ms, end_ms),
            )
            order_map = {str(row["orderId"]): row for row in orders}
            for trade in trades:
                order = order_map.get(str(trade["orderId"]))
                origin_class, origin_reason = classify_trade_origin(trade, order, registry)
                receipt = dict(
                    trade,
                    source="BINANCE_USER_TRADES",
                    origin_class=origin_class,
                    origin_reason=origin_reason,
                    accounting_authority=False,
                )
                token = f"{trade['symbol']}:{trade['id']}"
                key = "binance:accounting:trade:" + hashlib.sha256(token.encode()).hexdigest()[:32]
                encoded = json.dumps(receipt, sort_keys=True, separators=(",", ":"), allow_nan=False)
                previous = await db.load_key_value(key, strict=True)
                if previous != encoded:
                    if await db.save_key_value(key, encoded, strict=True) is not True:
                        raise db.PersistenceError("Binance trade evidence persistence unconfirmed")
                counts["trades"] += 1
                if origin_class == "BGX_ORDER_EVIDENCE":
                    counts["bgx_order_evidence"] += 1
                elif origin_class == "MANUAL_EXTERNAL":
                    counts["manual_external"] += 1
                else:
                    counts["unknown"] += 1

        log.warning(
            "[BINANCE_ACCOUNTING_EVIDENCE] status=PASS symbols=%s trades=%s "
            "bgx_order_evidence=%s manual_external=%s unknown=%s "
            "authority=false lifecycle_reconstruction=false execution_effect=NONE",
            len(symbols), counts["trades"], counts["bgx_order_evidence"],
            counts["manual_external"], counts["unknown"],
        )
        return counts
    except Exception as exc:
        log.warning(
            "[BINANCE_ACCOUNTING_EVIDENCE] status=UNCONFIRMED error_type=%s "
            "authority=false execution_effect=NONE",
            type(exc).__name__,
        )
        return None


async def audit_ledger(engine):
    """Persist recent futures income evidence; never changes risk/accounting authority."""
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - 48 * 3600000
    try:
        rows = await collect_income(engine.client, start_ms, end_ms)
        by_type: dict[str, dict[str, float | int]] = {}
        for row in rows:
            token = f"{row.get('incomeType')}:{row.get('tranId')}:{row.get('tradeId', '')}"
            key = "binance:accounting:income:" + hashlib.sha256(token.encode()).hexdigest()[:32]
            receipt = dict(row, source="BINANCE_INCOME", accounting_authority=False)
            encoded = json.dumps(receipt, sort_keys=True, separators=(",", ":"), allow_nan=False)
            previous = await db.load_key_value(key, strict=True)
            if previous != encoded:
                if await db.save_key_value(key, encoded, strict=True) is not True:
                    raise db.PersistenceError("Binance income evidence persistence unconfirmed")
            kind = str(row.get("incomeType") or "UNKNOWN")
            slot = by_type.setdefault(kind, {"count": 0, "income": 0.0})
            slot["count"] = int(slot["count"]) + 1
            slot["income"] = float(slot["income"]) + float(row.get("income", 0) or 0)

        log.warning(
            "[BINANCE_ACCOUNTING_LEDGER] status=PASS rows=%s types=%s "
            "authority=false execution_effect=NONE",
            len(rows),
            ",".join(
                f"{kind}:count={int(values['count'])}:income={float(values['income']):.8f}"
                for kind, values in sorted(by_type.items())
            ) or "NONE",
        )
        return rows
    except Exception as exc:
        log.warning(
            "[BINANCE_ACCOUNTING_LEDGER] status=UNCONFIRMED error_type=%s "
            "authority=false execution_effect=NONE",
            type(exc).__name__,
        )
        return None


async def _audit_all(engine):
    await audit(engine)
    now = time.monotonic()
    if now >= getattr(engine, "_binance_accounting_ledger_next", 0.0):
        engine._binance_accounting_ledger_next = now + 3600.0
        await audit_ledger(engine)


def schedule(engine):
    """Low-frequency read-only observation, isolated from PAPER decisions."""
    task = getattr(engine, "_binance_accounting_evidence_task", None)
    if task is not None and not task.done():
        return
    now = time.monotonic()
    interval = 3600.0 if getattr(engine, "paper_trade", True) else 600.0
    if now < getattr(engine, "_binance_accounting_evidence_next", 0.0):
        return
    engine._binance_accounting_evidence_next = now + interval
    task = asyncio.create_task(_audit_all(engine))
    engine._binance_accounting_evidence_task = task
    background = getattr(engine, "_background_tasks", None)
    if isinstance(background, set):
        background.add(task)
        task.add_done_callback(background.discard)
