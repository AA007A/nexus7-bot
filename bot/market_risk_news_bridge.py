"""Bridge structured US/crypto headline events into market-risk telemetry.

A headline alone cannot trigger the EXTREME gate because the deterministic
market-risk model caps macro-news contribution; it must agree with other fresh
risk signals such as whale flow or derivatives stress.
"""
from __future__ import annotations


def install(news_context, market_risk_runtime, log) -> None:
    if getattr(news_context, "_market_risk_news_bridge_installed", False):
        return

    original = news_context._event_snapshot_for_fresh_headlines
    macro_types = {
        "MACRO_CPI",
        "MACRO_FOMC",
        "MACRO_LABOR",
        "MACRO_GROWTH",
        "RATES",
        "US_EQUITY_STRESS",
        "WAR_GEOPOLITICS",
    }

    def snapshot_with_market_risk(log_arg, headlines):
        snap = original(log_arg, headlines)
        aggregate = snap.get("aggregate", {}) if isinstance(snap, dict) else {}
        types = set(aggregate.get("types", []) or [])
        severity = int(aggregate.get("severity", 0) or 0)
        if severity > 0 and types.intersection(macro_types):
            market_risk_runtime._merge_signals({"macro_event_severity": severity})
        return snap

    news_context._event_snapshot_for_fresh_headlines = snapshot_with_market_risk
    news_context._market_risk_news_bridge_installed = True
    log.info(
        "[MARKET_RISK_NEWS_BRIDGE] installed: structured macro/equity news severity "
        "feeds combined risk only; headline-alone execution authorization=false"
    )
