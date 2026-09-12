"""Regression coverage for KuCoin REST/WS kline activity-unit parity."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from bot import market_data_integrity as mdi


def test_rest_kline_uses_transaction_amount_index_6_without_mutating_input():
    raw = [[1789142400000, 100, 102, 99, 101, 7, 700.5]]
    normalized, rewritten, dropped = mdi._rewrite_kucoin_rest_kline_volume(raw)

    assert rewritten == 1
    assert dropped == 0
    assert normalized[0][5] == 700.5
    assert normalized[0][6] == 700.5
    assert raw[0][5] == 7


def test_rest_kline_drops_row_when_transaction_amount_is_missing_or_invalid():
    raw = [
        [1789142400000, 100, 102, 99, 101, 7],
        [1789143300000, 101, 103, 100, 102, 8, "nan"],
        [1789144200000, 102, 104, 101, 103, 9, -1],
    ]
    normalized, rewritten, dropped = mdi._rewrite_kucoin_rest_kline_volume(raw)

    assert normalized == []
    assert rewritten == 0
    assert dropped == 3


def test_rest_and_ws_normalize_to_same_activity_basis():
    amount = "12345.67"
    rest = [[1789142400000, 100, 102, 99, 101, 77, amount]]
    rest_fixed, _, _ = mdi._rewrite_kucoin_rest_kline_volume(rest)

    ws = {
        "topic": "/contractMarket/limitCandle:XBTUSDTM_15min",
        "type": "message",
        "data": {
            "candles": ["1789142400", "100", "101", "102", "99", "999999", amount]
        },
    }
    ws_fixed, changed = mdi._rewrite_kucoin_ws_kline_volume(ws)

    assert changed is True
    assert float(rest_fixed[0][5]) == float(ws_fixed["data"]["candles"][5])
    assert float(rest_fixed[0][5]) == float(amount)


def test_runtime_get_wrapper_normalizes_only_kline_query():
    class FakeClient:
        async def _get(self, endpoint, *args, **kwargs):
            if endpoint == "/api/v1/kline/query":
                return [[1789142400000, 100, 102, 99, 101, 7, 700.5]]
            return {"untouched": True}

        async def _handle_ws_message(self, msg):
            return msg

    class FakeAnalyzer:
        def analyze_mtf(self, *args, **kwargs):
            return None

    log = SimpleNamespace(
        warning=lambda *a, **k: None,
        error=lambda *a, **k: None,
    )
    mdi.install(FakeClient, FakeAnalyzer, log)
    client = FakeClient()

    kline = asyncio.run(client._get("/api/v1/kline/query"))
    other = asyncio.run(client._get("/api/v1/ticker"))

    assert kline[0][5] == 700.5
    assert other == {"untouched": True}


def test_volume_parity_hardening_does_not_change_trading_thresholds_or_leverage():
    source = open(mdi.__file__, "r", encoding="utf-8").read()
    assert "NEXUS_MIN_RR_NET" not in source
    assert "NEXUS_MIN_SCORE" not in source
    assert "MIN_ENTRY_SCORE" not in source
    assert "cfg.LEVERAGE" not in source
    assert "place_order" not in source
    assert "create_order" not in source
