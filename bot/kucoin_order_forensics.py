"""Read-only KuCoin order forensics.

Usage:
    python -m bot.kucoin_order_forensics <order_id>

This module performs authenticated GET requests only.  It never creates,
amends, cancels, or closes an order.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

from bot.kucoin import KuCoinClient

_SAFE_ORDER_FIELDS = (
    "id", "orderId", "clientOid", "symbol", "side", "type", "status",
    "isActive", "cancelExist", "stop", "stopPriceType", "stopTriggered",
    "stopPrice", "price", "avgDealPrice", "size", "filledSize", "dealSize",
    "dealValue", "fee", "feeCurrency", "reduceOnly", "closeOrder",
    "createdAt", "updatedAt", "orderTime", "settleCurrency",
)
_SAFE_FILL_FIELDS = (
    "tradeId", "orderId", "symbol", "side", "price", "size", "value",
    "fee", "feeRate", "feeCurrency", "liquidity", "createdAt", "tradeTime",
)


def _pick(row: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    return {key: row.get(key) for key in fields if key in row}


async def snapshot_order(order_id: str) -> dict[str, Any]:
    if not order_id or not order_id.isdigit():
        raise ValueError("order_id must be a non-empty numeric KuCoin order id")

    client = KuCoinClient()
    try:
        order = await client.get_order_status(order_id)
        if not isinstance(order, dict) or not order:
            raise RuntimeError("ORDER_NOT_CONFIRMED")

        fills_payload = await client._get(
            "/api/v1/fills",
            {"orderId": order_id},
            auth=True,
        )
        rows: list[dict[str, Any]] = []
        if isinstance(fills_payload, dict):
            items = fills_payload.get("items", [])
            if isinstance(items, list):
                rows = [
                    _pick(item, _SAFE_FILL_FIELDS)
                    for item in items
                    if isinstance(item, dict)
                    and str(item.get("orderId", "")) == order_id
                ]

        return {
            "forensic_mode": "READ_ONLY",
            "execution_effect": "NONE",
            "order": _pick(order, _SAFE_ORDER_FIELDS),
            "fills": rows,
            "fills_count": len(rows),
        }
    finally:
        session = getattr(client, "_session", None)
        if session is not None and not session.closed:
            await session.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only KuCoin order forensics")
    parser.add_argument("order_id")
    args = parser.parse_args()
    result = asyncio.run(snapshot_order(args.order_id))
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
