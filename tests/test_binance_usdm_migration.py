"""Offline Binance USD-M migration regressions.

No Binance credentials and no network access are used.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

from bot import binance as bn
from bot.conditional_stop_protection import conditional_stop_confirmed


def run(coro):
    return asyncio.run(coro)


class FakeBinance(bn.BinanceClient):
    def __init__(self, responses=None):
        super().__init__()
        self.responses = responses or {}
        self.calls = []

    async def _get(self, endpoint, params=None, auth=False):
        self.calls.append(("GET", endpoint, params or {}, auth))
        value = self.responses.get(endpoint, {})
        if callable(value):
            value = value(params or {}, auth)
        return value

    async def close(self):
        return None


class BinanceMigrationTests(unittest.TestCase):
    def test_required_engine_interface_is_present(self):
        required = {
            "get_balance", "load_instruments", "get_instruments",
            "set_leverage", "place_order", "set_position_stops", "set_sl",
            "cancel_all_orders", "get_klines", "get_cached_klines",
            "get_ticker", "get_cached_ticker", "get_all_tickers",
            "get_open_interest", "get_funding_rate", "get_order_status",
            "wait_for_fill", "get_orderbook", "get_positions",
            "start_websocket", "start_private_websocket", "get_cache_stats",
            "sync_time", "ping", "close",
        }
        missing = sorted(name for name in required if not hasattr(bn.BinanceClient, name))
        self.assertEqual(missing, [])

    def test_exchange_info_maps_base_quantity_units(self):
        exchange_info = {
            "symbols": [
                {
                    "symbol": "BTCUSDT",
                    "contractType": "PERPETUAL",
                    "quoteAsset": "USDT",
                    "status": "TRADING",
                    "filters": [
                        {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                        {"filterType": "MARKET_LOT_SIZE", "minQty": "0.001", "stepSize": "0.001"},
                        {"filterType": "MIN_NOTIONAL", "notional": "5"},
                    ],
                },
                {
                    "symbol": "ETHUSDT",
                    "contractType": "PERPETUAL",
                    "quoteAsset": "USDT",
                    "status": "TRADING",
                    "filters": [
                        {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                        {"filterType": "MARKET_LOT_SIZE", "minQty": "0.001", "stepSize": "0.001"},
                        {"filterType": "MIN_NOTIONAL", "notional": "5"},
                    ],
                },
            ]
        }
        client = FakeBinance({
            "/fapi/v1/time": {"serverTime": 1_800_000_000_000},
            "/fapi/v1/exchangeInfo": exchange_info,
        })
        instruments = run(client.load_instruments())
        self.assertEqual(instruments["BTCUSDT"]["multiplier"], 1.0)
        self.assertEqual(instruments["BTCUSDT"]["quantityUnit"], "BASE_ASSET")
        self.assertEqual(instruments["BTCUSDT"]["qtyStep"], 0.001)
        self.assertEqual(instruments["BTCUSDT"]["tickSize"], 0.1)

    def test_quantity_floors_to_step_and_price_aligns_to_tick(self):
        client = FakeBinance()
        client._instruments = {
            "BTCUSDT": {
                "qtyStep": 0.001,
                "minQty": 0.001,
                "tickSize": 0.1,
            }
        }
        self.assertEqual(client._round_qty(0.00199, "BTCUSDT"), "0.001")
        self.assertEqual(client._round_price(101.24, "BTCUSDT"), "101.2")
        with self.assertRaises(ValueError):
            client._round_qty(0.0009, "BTCUSDT")

    def test_positions_are_normalized_as_base_asset_and_hedge_duplicates_block(self):
        rows = [
            {
                "symbol": "BTCUSDT", "positionAmt": "0.015",
                "entryPrice": "60000", "markPrice": "60100",
                "unRealizedProfit": "1.5", "leverage": "10",
                "liquidationPrice": "52000", "isolatedMargin": "0",
                "positionSide": "BOTH", "marginType": "cross",
            }
        ]
        client = FakeBinance({"/fapi/v3/positionRisk": rows})
        positions = run(client.get_positions())
        self.assertEqual(positions[0]["size"], 0.015)
        self.assertEqual(positions[0]["sizeUnit"], "BASE_ASSET")
        self.assertEqual(positions[0]["side"], "Buy")

        client.responses["/fapi/v3/positionRisk"] = [
            rows[0],
            dict(rows[0], positionAmt="-0.010", positionSide="SHORT"),
        ]
        with self.assertRaisesRegex(RuntimeError, "HEDGE_MODE_UNSUPPORTED"):
            run(client.get_positions())

    def test_klines_are_chronological_and_normalized(self):
        raw = [
            [2_000, "2", "3", "1", "2.5", "20", 0, 0, 0, 0, 0, 0],
            [1_000, "1", "2", "0.5", "1.5", "10", 0, 0, 0, 0, 0, 0],
        ]
        client = FakeBinance({"/fapi/v1/klines": raw})
        rows = run(client.get_klines("BTCUSDT", "15", 10))
        self.assertEqual([row["ts"] for row in rows], [1_000, 2_000])
        self.assertEqual(rows[1]["v"], 20.0)

    def test_account_state_uses_margin_equity_and_available_balance(self):
        client = FakeBinance({
            "/fapi/v3/account": {
                "totalMarginBalance": "125.50",
                "totalWalletBalance": "120.00",
                "availableBalance": "80.25",
                "totalUnrealizedProfit": "5.50",
                "totalPositionInitialMargin": "30",
                "totalOpenOrderInitialMargin": "2",
            }
        })
        state = run(client.get_account_state())
        self.assertEqual(state["equity"], 125.5)
        self.assertEqual(state["available"], 80.25)
        self.assertEqual(state["available_source"], "availableBalance")

    def test_conditional_close_stop_is_recognized_without_contract_conversion(self):
        client = FakeBinance({
            "/fapi/v1/openAlgoOrders": [
                {
                    "symbol": "BTCUSDT",
                    "side": "SELL",
                    "algoStatus": "NEW",
                    "triggerPrice": "59000",
                    "closePosition": True,
                    "algoId": 99,
                    "clientAlgoId": "bgx7-stop",
                }
            ]
        })
        client._instruments = {
            "BTCUSDT": {
                "multiplier": 1.0, "minQty": 0.001,
                "qtyStep": 0.001, "tickSize": 0.1,
            }
        }
        position = {
            "symbol": "BTCUSDT", "side": "Buy", "size": 0.01,
            "sizeUnit": "BASE_ASSET", "entryPrice": 60000,
            "markPrice": 60010, "stopLoss": 0,
        }
        protected, evidence = run(conditional_stop_confirmed(client, position))
        self.assertTrue(protected)
        self.assertEqual(evidence, "conditional_close_order")

    def test_paper_order_is_synthetic_and_never_calls_network(self):
        client = FakeBinance()
        result = run(client.place_order(
            "BTCUSDT", "Buy", 0.01, sl=59000, tp=62000
        ))
        self.assertTrue(str(result["orderId"]).startswith("paper_"))
        self.assertTrue(result["clientOid"].startswith("bgx7-"))
        self.assertLessEqual(len(result["clientOid"]), 36)
        self.assertEqual(client.calls, [])

    def test_binance_live_migration_gate_fails_closed(self):
        client = FakeBinance()
        old = bn.PAPER_TRADE
        try:
            bn.PAPER_TRADE = False
            with patch.dict(os.environ, {"BINANCE_LIVE_MIGRATION_READY": "false"}, clear=False):
                with self.assertRaisesRegex(RuntimeError, "BINANCE_LIVE_MIGRATION_NOT_RELEASED"):
                    run(client.set_leverage("BTCUSDT", 10))
        finally:
            bn.PAPER_TRADE = old

    def test_binance_hardened_entrypoint_imports_in_paper(self):
        env = os.environ.copy()
        env.update({
            "EXCHANGE": "binance",
            "PAPER_TRADE": "true",
            "PAPER_INITIAL_BALANCE": "1000",
            "BINANCE_LIVE_MIGRATION_READY": "false",
            "LIVE_TRADING_CONFIRMED": "",
            "REAL_TRADING_PILOT": "false",
            "LOG_LEVEL": "ERROR",
        })
        proc = subprocess.run(
            [sys.executable, "-c", "import main_hardened; print('binance-paper-import-ok')"],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(
            proc.returncode, 0,
            msg=(proc.stdout + "\n" + proc.stderr)[-5000:],
        )
        self.assertIn("binance-paper-import-ok", proc.stdout)

    def test_funding_open_interest_and_ticker_normalization(self):
        client = FakeBinance({
            "/fapi/v1/ticker/24hr": {
                "lastPrice": "100", "bidPrice": "99.9", "askPrice": "100.1",
                "volume": "20", "quoteVolume": "2000",
            },
            "/fapi/v1/openInterest": {"openInterest": "12.5"},
            "/fapi/v1/premiumIndex": {"lastFundingRate": "0.0001"},
        })
        ticker = run(client.get_ticker("BTCUSDT"))
        oi = run(client.get_open_interest("BTCUSDT"))
        funding = run(client.get_funding_rate("BTCUSDT"))
        self.assertEqual(ticker["turnover"], 2000.0)
        self.assertEqual(float(oi["openInterestValue"]), 1250.0)
        self.assertEqual(funding, 0.0001)


if __name__ == "__main__":
    unittest.main()
