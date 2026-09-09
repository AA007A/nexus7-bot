import math
from bot.market_risk_intelligence import assess_market_risk

def test_empty_and_malformed_inputs_are_fail_neutral():
    empty=assess_market_risk({});assert empty.score==0;assert empty.level=="NORMAL";assert empty.block_new_entries is False
    malformed=assess_market_risk({"liquidation_usd_1h":math.nan,"open_interest_change_pct":math.inf,"funding_rate_pct":True,"dxy_change_pct":"bad"})
    assert malformed.score==0

def test_cross_asset_risk_off_combination_can_reach_extreme():
    out=assess_market_risk({"spx_change_pct":-2.4,"ndx_change_pct":-3.0,"vix_change_pct":15.0,"dxy_change_pct":1.2,"us2y_yield_change_bps":18.0,"us10y_yield_change_bps":16.0,"macro_event_severity":90})
    assert out.score==100;assert out.level=="EXTREME";assert out.block_new_entries is True
    for reason in ("SPX_RISK_OFF","NDX_RISK_OFF","VIX_SPIKE","DXY_SURGE","US2Y_YIELD_SHOCK","US10Y_YIELD_SHOCK","MACRO_EVENT_EXTREME"):assert reason in out.reasons

def test_single_macro_indicator_does_not_authorize_or_block():
    out=assess_market_risk({"dxy_change_pct":1.1})
    assert out.score==12;assert out.block_new_entries is False

def test_derivatives_plus_macro_can_block():
    out=assess_market_risk({"liquidation_usd_1h":600_000_000,"open_interest_change_pct":13.0,"funding_rate_pct":0.10,"spx_change_pct":-2.2,"macro_event_severity":90})
    assert out.score>=85;assert out.block_new_entries is True

def test_positive_risk_on_moves_are_not_misclassified_as_risk_off():
    out=assess_market_risk({"spx_change_pct":2.0,"ndx_change_pct":2.5,"vix_change_pct":-12.0,"dxy_change_pct":-1.0,"us10y_yield_change_bps":-12.0})
    assert out.score==0;assert out.level=="NORMAL"
