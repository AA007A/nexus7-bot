import asyncio

import pytest

from bot import binance_accounting_evidence as accounting


class FakeClient:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    async def _get(self, endpoint, params=None, auth=False):
        self.calls.append((endpoint, dict(params or {}), auth))
        value = self.responses[endpoint]
        if callable(value):
            return value(dict(params or {}))
        return value


def run(coro):
    return asyncio.run(coro)


def test_collect_user_trades_normalizes_and_requires_identity():
    client = FakeClient({
        "/fapi/v1/userTrades": [
            {
                "symbol": "BTCUSDT",
                "id": 7,
                "orderId": 11,
                "side": "BUY",
                "price": "100",
                "qty": "0.1",
                "quoteQty": "10",
                "realizedPnl": "0",
                "commission": "0.004",
                "commissionAsset": "USDT",
                "time": 1500,
                "buyer": True,
                "maker": False,
                "positionSide": "BOTH",
                "ignored": "secret-noise",
            }
        ]
    })
    rows = run(accounting.collect_user_trades(client, "BTCUSDT", 1000, 2000))
    assert rows == [{
        "symbol": "BTCUSDT",
        "id": 7,
        "orderId": 11,
        "side": "BUY",
        "price": "100",
        "qty": "0.1",
        "quoteQty": "10",
        "realizedPnl": "0",
        "commission": "0.004",
        "commissionAsset": "USDT",
        "time": 1500,
        "buyer": True,
        "maker": False,
        "positionSide": "BOTH",
    }]
    assert client.calls[0][2] is True


def test_collect_user_trades_refuses_unproven_full_page():
    rows = [
        {"symbol": "BTCUSDT", "id": i, "orderId": i, "time": 1500}
        for i in range(1000)
    ]
    client = FakeClient({"/fapi/v1/userTrades": rows})
    with pytest.raises(ValueError, match="coverage"):
        run(accounting.collect_user_trades(client, "BTCUSDT", 1000, 2000))


def test_collect_income_pages_and_deduplicates():
    def response(params):
        if params["page"] == 1:
            return [
                {
                    "symbol": "BTCUSDT",
                    "incomeType": "REALIZED_PNL",
                    "income": "1.25",
                    "asset": "USDT",
                    "info": "",
                    "time": 1500,
                    "tranId": 9,
                    "tradeId": "7",
                }
            ]
        return []

    client = FakeClient({"/fapi/v1/income": response})
    rows = run(accounting.collect_income(client, 1000, 2000))
    assert len(rows) == 1
    assert rows[0]["incomeType"] == "REALIZED_PNL"


def test_bgx_order_evidence_never_becomes_bgx_confirmed():
    trade = {"symbol": "BTCUSDT", "orderId": 11, "side": "BUY"}
    order = {"symbol": "BTCUSDT", "orderId": 11, "clientOrderId": "bgx7-entry-abc"}
    registry = [{"order_id": 11, "symbol": "BTCUSDT"}]
    origin, reason = accounting.classify_trade_origin(trade, order, registry)
    assert origin == "BGX_ORDER_EVIDENCE"
    assert origin != "BGX_CONFIRMED"
    assert reason == "BGX_CLIENT_ORDER_ID_AND_DURABLE_ORDER"


def test_non_bgx_order_without_durable_registry_is_manual_external():
    trade = {"symbol": "BTCUSDT", "orderId": 22, "side": "SELL"}
    order = {"symbol": "BTCUSDT", "orderId": 22, "clientOrderId": "manual-order"}
    origin, reason = accounting.classify_trade_origin(trade, order, [])
    assert origin == "MANUAL_EXTERNAL"
    assert reason == "NON_BGX_CLIENT_ORDER_ID_CONFIRMED"


def test_durable_order_with_non_bgx_client_id_fails_closed():
    trade = {"symbol": "BTCUSDT", "orderId": 33, "side": "SELL"}
    order = {"symbol": "BTCUSDT", "orderId": 33, "clientOrderId": "manual-order"}
    registry = [{"order_id": 33, "symbol": "BTCUSDT"}]
    origin, reason = accounting.classify_trade_origin(trade, order, registry)
    assert origin == "UNKNOWN_UNATTRIBUTED"
    assert reason == "DURABLE_ORDER_CONFLICTS_WITH_NON_BGX_CLIENT_ID"


def test_invalid_window_over_seven_days_fails_closed():
    client = FakeClient({"/fapi/v1/userTrades": []})
    with pytest.raises(ValueError, match="window"):
        run(accounting.collect_user_trades(
            client, "BTCUSDT", 1000, 1000 + 7 * 86400000 + 1
        ))
