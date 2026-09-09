import time

from bot import market_risk_runtime as runtime

# Collector tests cover retained free sources only: CoinGlass, cross-asset prices,
# official U.S. Treasury yields, and the existing public macro/news bridge.


def _reset_runtime_state():
    """Reset runtime state used by the retained-provider test suite."""
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
        "data": [{
            "symbol": "BTC",
            "open_interest_usd": 10_000_000_000,
            "avg_funding_rate_by_oi": 0.01,
        }],
    }, now=1000)
    assert "open_interest_change_pct" not in first

    second = runtime.parse_coinglass_markets({
        "code": "0",
        "data": [{
            "symbol": "BTC",
            "open_interest_usd": 11_200_000_000,
            "avg_funding_rate_by_oi": 0.02,
        }],
    }, now=1300)
    assert round(second["open_interest_change_pct"], 2) == 12.0


def test_chart_change_normalizes_percent_move():
    out = runtime.parse_chart_change({
        "chart": {
            "result": [{
                "indicators": {
                    "quote": [{"close": [100.0, 102.5]}]
                }
            }]
        }
    })
    assert round(out, 2) == 2.5


def test_treasury_curve_uses_actual_2y_and_10y_yields():
    xml = """<?xml version='1.0' encoding='utf-8'?>
    <feed xmlns='http://www.w3.org/2005/Atom'
          xmlns:d='http://schemas.microsoft.com/ado/2007/08/dataservices'
          xmlns:m='http://schemas.microsoft.com/ado/2007/08/dataservices/metadata'>
      <entry><content type='application/xml'><m:properties>
        <d:NEW_DATE>2026-09-08T00:00:00</d:NEW_DATE>
        <d:BC_2YEAR>4.31</d:BC_2YEAR>
        <d:BC_10YEAR>4.80</d:BC_10YEAR>
      </m:properties></content></entry>
      <entry><content type='application/xml'><m:properties>
        <d:NEW_DATE>2026-09-09T00:00:00</d:NEW_DATE>
        <d:BC_2YEAR>4.42</d:BC_2YEAR>
        <d:BC_10YEAR>4.83</d:BC_10YEAR>
      </m:properties></content></entry>
    </feed>"""
    out = runtime.parse_treasury_curve_xml(xml)
    assert round(out["us2y_yield_change_bps"], 2) == 11.0
    assert round(out["us10y_yield_change_bps"], 2) == 3.0


def test_treasury_curve_malformed_xml_is_fail_neutral():
    assert runtime.parse_treasury_curve_xml("<bad") == {}


def test_signal_expiry_is_independent_per_signal():
    _reset_runtime_state()
    now = time.time()
    runtime._merge_signals({"liquidation_usd_1h": 600_000_000}, now=now - 1300)
    runtime._merge_signals({"macro_event_severity": 60}, now=now - 100)

    snap = runtime.snapshot(now=now)
    assert "liquidation_usd_1h" not in snap["signals"]
    assert snap["signals"]["macro_event_severity"] == 60
    assert snap["assessment"].block_new_entries is False


def test_combined_fresh_risk_can_block_pilot_gate_with_free_macro_sources():
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
        "us2y_yield_change_bps": 18.0,
        "us10y_yield_change_bps": 16.0,
    }, now=now)

    guard = DummyPilot()
    reasons = guard.evaluate(None, None, "BTCUSDT")
    assert "BASE_GATE" in reasons
    assert any(reason.startswith("15_MARKET_RISK: EXTREME") for reason in reasons)
