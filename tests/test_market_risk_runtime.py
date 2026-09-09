import time

from bot import market_risk_runtime as runtime


def _reset_runtime_state():
    runtime._state["signals"] = {}
    runtime._state["signal_updated_at"] = {}
    runtime._state["providers"] = {}
    runtime._previous_coinglass_oi = None


def test_whale_alert_counts_only_attributed_exchange_flow():
    _reset_runtime_state()
    inflow = runtime.parse_whale_alert({
        "type": "alert",
        "from": "unknown wallet",
        "to": "binance",
        "amounts": [{"symbol": "BTC", "value_usd": 40_000_000}],
    })
    assert inflow == {"whale_exchange_inflow_usd": 40_000_000}

    unknown = runtime.parse_whale_alert({
        "type": "alert",
        "from": "unknown wallet",
        "to": "unknown wallet",
        "amounts": [{"symbol": "BTC", "value_usd": 300_000_000}],
    })
    assert unknown == {}


def test_coinglass_liquidation_uses_aggregate_row():
    _reset_runtime_state()
    out = runtime.parse_coinglass_liquidation({
        "code": "0",
        "data": [
            {"exchange": "All", "liquidation_usd": 175_000_000},
            {"exchange": "Binance", "liquidation_usd": 80_000_000},
        ],
    })
    assert out == {"liquidation_usd_1h": 175_000_000}


def test_coinglass_open_interest_change_requires_two_samples():
    _reset_runtime_state()
    first = runtime.parse_coinglass_markets({
        "code": "0",
        "data": [{"symbol": "BTC", "open_interest_usd": 10_000_000_000, "avg_funding_rate_by_oi": 0.01}],
    }, now=1000)
    assert "open_interest_change_pct" not in first

    second = runtime.parse_coinglass_markets({
        "code": "0",
        "data": [{"symbol": "BTC", "open_interest_usd": 11_200_000_000, "avg_funding_rate_by_oi": 0.02}],
    }, now=1300)
    assert round(second["open_interest_change_pct"], 2) == 12.0


def test_cryptoquant_reserve_change_is_normalized():
    _reset_runtime_state()
    out = runtime.parse_cryptoquant_reserve({
        "result": {"data": [
            {"reserve_usd": 100_000_000_000},
            {"reserve_usd": 102_500_000_000},
        ]}
    })
    assert round(out["btc_exchange_reserve_change_pct"], 2) == 2.5


def test_signal_expiry_is_independent_per_signal():
    _reset_runtime_state()
    now = time.time()
    runtime._merge_signals({"whale_exchange_inflow_usd": 150_000_000}, now=now - 700)
    runtime._merge_signals({"btc_exchange_reserve_change_pct": 2.5}, now=now - 100)

    snap = runtime.snapshot(now=now)
    assert "whale_exchange_inflow_usd" not in snap["signals"]
    assert snap["signals"]["btc_exchange_reserve_change_pct"] == 2.5
    assert snap["assessment"].block_new_entries is False


def test_combined_fresh_risk_can_block_pilot_gate():
    _reset_runtime_state()

    class DummyState:
        blocked_reasons = []

    class DummyPilot:
        _market_risk_intelligence_installed = False
        def __init__(self):
            self.state = DummyState()
        def evaluate(self, engine, client, symbol, ai_decision=None):
            return ["BASE_GATE"]

    class DummyScoring:
        @staticmethod
        async def news_reader_loop():
            return None

    class DummyLog:
        def warning(self, *args, **kwargs):
            return None

    runtime.install(DummyPilot, DummyScoring, DummyLog())
    now = time.time()
    runtime._merge_signals({
        "whale_exchange_inflow_usd": 125_000_000,
        "btc_exchange_netflow_usd": 175_000_000,
        "liquidation_usd_1h": 600_000_000,
        "macro_event_severity": 90,
    }, now=now)

    guard = DummyPilot()
    reasons = guard.evaluate(None, None, "BTCUSDT")
    assert "BASE_GATE" in reasons
    assert any(reason.startswith("15_MARKET_RISK: EXTREME") for reason in reasons)
