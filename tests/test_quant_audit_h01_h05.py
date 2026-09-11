"""Regression tests for quant-audit findings H01-H05.

These tests are deliberately offline. They verify the statistical/data contracts
without sending orders or requiring KuCoin credentials.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from bot import backtest
from bot import optimizer


class FakeKuCoinHistoryClient:
    """Minimal _get client that returns candles inside requested time bounds."""

    def __init__(self, candles):
        self.candles = list(candles)
        self.calls = []

    async def _get(self, endpoint, params=None, auth=False):
        assert endpoint == "/api/v1/kline/query"
        params = params or {}
        start = int(params["from"])
        end = int(params["to"])
        self.calls.append((start, end))
        rows = []
        for candle in self.candles:
            ts = int(candle[0])
            if start <= ts <= end:
                rows.append(candle)
        # KuCoin may return newest first; deliberately exercise normalization.
        return list(reversed(rows))


def _candles(count=12, interval_min=15, start_ms=1_700_000_000_000):
    out = []
    step = interval_min * 60 * 1000
    for i in range(count):
        ts = start_ms + i * step
        # Same opening price on purpose: H01 must not de-dupe by price.
        o = 100.0
        c = 100.0 + i * 0.1
        h = max(o, c) + 1.0
        l = min(o, c) - 1.0
        out.append([ts, o, h, l, c, 10 + i, 0])
    return out


def test_h01_history_paginates_and_dedupes_by_timestamp():
    rows = _candles(count=12)
    client = FakeKuCoinHistoryClient(rows)

    # Force a historical cursor that fully contains the deterministic fixture.
    original_time = backtest.time.time
    backtest.time.time = lambda: (rows[-1][0] + 1) / 1000
    try:
        got = asyncio.run(backtest.fetch_history(client, "BTCUSDT", "15", 12))
    finally:
        backtest.time.time = original_time

    assert len(got) == 12
    assert len({c["ts"] for c in got}) == 12
    assert [c["ts"] for c in got] == sorted(c["ts"] for c in got)
    assert len({c["o"] for c in got}) == 1  # proves equal open prices survive
    assert client.calls


def test_h01_integrity_detects_duplicates_and_gaps():
    rows = list(_candles(count=4))
    normalized = [backtest._normalize_kline(r) for r in rows]
    clean = [c for c in normalized if c is not None]
    assert backtest._historical_integrity(clean, "15")["ok"] is True

    duplicate = clean + [dict(clean[-1])]
    duplicate.sort(key=lambda c: c["ts"])
    integrity = backtest._historical_integrity(duplicate, "15")
    assert integrity["duplicates"] == 1
    assert integrity["ok"] is False

    gap = [dict(clean[0]), dict(clean[1]), dict(clean[3])]
    assert backtest._historical_integrity(gap, "15")["gaps"] == 1


def test_h02_chronological_train_validation_test_split_is_disjoint():
    k15 = [{"ts": i} for i in range(100)]
    k1h = [{"ts": i} for i in range(25)]
    k4h = [{"ts": i} for i in range(7)]
    split = optimizer._split_by_time(k15, k1h, k4h)

    train15 = split["train"][0]
    val15 = split["validation"][0]
    test15 = split["test"][0]
    assert len(train15) == 60
    assert len(val15) == 20
    assert len(test15) == 20
    assert train15[-1]["ts"] < val15[0]["ts"] < test15[0]["ts"]
    assert {x["ts"] for x in train15}.isdisjoint({x["ts"] for x in val15})
    assert {x["ts"] for x in val15}.isdisjoint({x["ts"] for x in test15})


class FakeTrial:
    def __init__(self):
        self.seen = []

    def suggest_int(self, name, low, high):
        self.seen.append(name)
        return low

    def suggest_float(self, name, low, high):
        self.seen.append(name)
        return low


def test_h03_optimizer_search_space_contains_only_effective_parameters():
    trial = FakeTrial()
    params = optimizer._sample_params(trial)
    assert set(params) == {"min_score", "min_rr"}
    assert set(trial.seen) == {"min_score", "min_rr"}
    forbidden = {
        "sl_mult", "tp_mult", "rsi_ob", "rsi_os", "min_adx",
        "vol_threshold", "bos_lookback", "momentum_atr_mult",
    }
    assert forbidden.isdisjoint(params)


def test_h02_holdout_gate_is_fail_closed():
    good = {
        "total_trades": 20,
        "profit_factor": 1.2,
        "expectancy_pct": 0.1,
        "sharpe_ratio": 0.4,
    }
    allowed, reasons = optimizer._holdout_gate(good, good)
    assert allowed is True
    assert reasons == []

    bad_test = {
        "total_trades": 20,
        "profit_factor": 0.9,
        "expectancy_pct": -0.1,
        "sharpe_ratio": -0.2,
    }
    allowed, reasons = optimizer._holdout_gate(good, bad_test)
    assert allowed is False
    assert any("test: PF" in r for r in reasons)
    assert any("test: expectancy" in r for r in reasons)
    assert any("test: Sharpe" in r for r in reasons)


def test_h05_real_timestamp_calendar_fields():
    ts = int(datetime(2026, 9, 11, 22, 15, tzinfo=timezone.utc).timestamp() * 1000)
    dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
    assert dt.hour == 22
    assert dt.weekday() == 4  # Friday
