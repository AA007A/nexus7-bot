import time

from bot import market_risk_runtime as runtime


def _reset_runtime_state():
    runtime._state["signals"] = {}
    runtime._state["signal_updated_at"] = {}
    runtime._state["providers"] = {}
    runtime._previous_coinglass_oi = None


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


def test_signal_expiry_is_independent_per_signal():
    _reset_runtime_state()
    now = time.time()
    runtime._merge_signals({"liquidation_usd_1h": 600_000_000}, now=now - 1300)
    runtime._merge_signals({"macro_event_severity": 60}, now=now - 100)

    snap = runtime.snapshot(now=now)
    assert "liquidation_usd_1h" not in snap["signals"]
    assert snap["signals"]["macro_event_severity"] == 60
    assert snap["assessment"].block_new_entries is False


def test_combined_fresh_risk_can_block_pilot_gate_with_coinglass_and_macro():
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
        "liquidation_usd_1h": 600_000_000,
        "open_interest_change_pct": 15.0,
        "funding_rate_pct": 0.15,
        "macro_event_severity": 90,
        "vix_change_pct": 15.0,
    }, now=now)

    guard = DummyPilot()
    reasons = guard.evaluate(None, None, "BTCUSDT")
    assert "BASE_GATE" in reasons
    assert any(reason.startswith("15_MARKET_RISK: EXTREME") for reason in reasons)
