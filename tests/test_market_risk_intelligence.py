import math

from bot.market_risk_intelligence import assess_market_risk


def test_empty_and_malformed_inputs_are_fail_neutral():
    empty = assess_market_risk({})
    assert empty.score == 0
    assert empty.level == "NORMAL"
    assert empty.block_new_entries is False
    assert empty.size_multiplier == 1.0

    malformed = assess_market_risk({
        "whale_exchange_inflow_usd": "bad",
        "liquidation_usd_1h": math.nan,
        "open_interest_change_pct": math.inf,
        "funding_rate_pct": True,
    })
    assert malformed.score == 0
    assert malformed.block_new_entries is False


def test_single_whale_signal_raises_risk_but_does_not_block():
    out = assess_market_risk({"whale_exchange_inflow_usd": 125_000_000})
    assert out.score == 30
    assert out.level == "WATCH"
    assert out.block_new_entries is False
    assert "WHALE_EXCHANGE_INFLOW_EXTREME" in out.reasons


def test_combined_dislocation_signals_block_extreme_risk():
    out = assess_market_risk({
        "whale_exchange_inflow_usd": 125_000_000,
        "btc_exchange_netflow_usd": 175_000_000,
        "liquidation_usd_1h": 600_000_000,
        "spx_change_pct": -2.4,
        "macro_event_severity": 90,
    })
    assert out.score == 100
    assert out.level == "EXTREME"
    assert out.block_new_entries is True
    assert out.size_multiplier == 0.0


def test_high_risk_reduces_recommended_size_without_blocking():
    out = assess_market_risk({
        "whale_exchange_inflow_usd": 30_000_000,
        "liquidation_usd_1h": 180_000_000,
        "open_interest_change_pct": 13.0,
        "funding_rate_pct": 0.10,
        "spx_change_pct": -1.2,
    })
    assert 65 <= out.score < 85
    assert out.level == "HIGH"
    assert out.block_new_entries is False
    assert out.size_multiplier == 0.50


def test_large_whale_outflow_never_makes_score_negative():
    out = assess_market_risk({"whale_exchange_outflow_usd": 500_000_000})
    assert out.score == 0
    assert out.level == "NORMAL"
