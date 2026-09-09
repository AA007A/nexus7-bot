"""Deterministic market-risk intelligence for NEXUS-7.

This module normalizes whale/on-chain/derivatives/macro observations into a
conservative risk score. It never authorizes an order. Missing providers are
fail-neutral; malformed provider data is ignored rather than converted into a
fabricated signal.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Any


@dataclass(frozen=True)
class MarketRiskAssessment:
    score: int
    level: str
    block_new_entries: bool
    size_multiplier: float
    reasons: tuple[str, ...] = field(default_factory=tuple)


def _num(data: Mapping[str, Any], key: str) -> float | None:
    try:
        value = data.get(key)
        if value is None or isinstance(value, bool):
            return None
        out = float(value)
        if out != out or out in (float("inf"), float("-inf")):
            return None
        return out
    except (TypeError, ValueError, OverflowError):
        return None


def assess_market_risk(signals: Mapping[str, Any] | None) -> MarketRiskAssessment:
    """Return a conservative 0..100 risk assessment.

    Supported normalized inputs are intentionally provider-agnostic:
      whale_exchange_inflow_usd, whale_exchange_outflow_usd,
      btc_exchange_netflow_usd, btc_exchange_reserve_change_pct,
      liquidation_usd_1h, open_interest_change_pct, funding_rate_pct,
      spx_change_pct, vix_change_pct, macro_event_severity.

    The score estimates *market manipulation / dislocation risk*. It does not
    claim that manipulation has occurred.
    """
    data = signals or {}
    score = 0
    reasons: list[str] = []

    whale_in = _num(data, "whale_exchange_inflow_usd")
    whale_out = _num(data, "whale_exchange_outflow_usd")
    if whale_in is not None and whale_in >= 100_000_000:
        score += 30
        reasons.append("WHALE_EXCHANGE_INFLOW_EXTREME")
    elif whale_in is not None and whale_in >= 25_000_000:
        score += 18
        reasons.append("WHALE_EXCHANGE_INFLOW_HIGH")
    if whale_out is not None and whale_out >= 100_000_000:
        score = max(0, score - 8)
        reasons.append("WHALE_EXCHANGE_OUTFLOW_HIGH")

    netflow = _num(data, "btc_exchange_netflow_usd")
    if netflow is not None and netflow >= 150_000_000:
        score += 25
        reasons.append("BTC_NETFLOW_TO_EXCHANGES_EXTREME")
    elif netflow is not None and netflow >= 50_000_000:
        score += 15
        reasons.append("BTC_NETFLOW_TO_EXCHANGES_HIGH")

    reserve_delta = _num(data, "btc_exchange_reserve_change_pct")
    if reserve_delta is not None and reserve_delta >= 2.0:
        score += 12
        reasons.append("BTC_EXCHANGE_RESERVES_RISING")

    liq = _num(data, "liquidation_usd_1h")
    if liq is not None and liq >= 500_000_000:
        score += 30
        reasons.append("LIQUIDATION_CASCADE_EXTREME")
    elif liq is not None and liq >= 150_000_000:
        score += 18
        reasons.append("LIQUIDATION_CASCADE_HIGH")

    oi = _num(data, "open_interest_change_pct")
    funding = _num(data, "funding_rate_pct")
    if oi is not None and abs(oi) >= 12.0:
        score += 15
        reasons.append("OPEN_INTEREST_DISLOCATION")
    elif oi is not None and abs(oi) >= 7.0:
        score += 8
        reasons.append("OPEN_INTEREST_ELEVATED")
    if funding is not None and abs(funding) >= 0.08:
        score += 12
        reasons.append("FUNDING_EXTREME")

    spx = _num(data, "spx_change_pct")
    vix = _num(data, "vix_change_pct")
    if spx is not None and spx <= -2.0:
        score += 18
        reasons.append("US_EQUITY_RISK_OFF")
    elif spx is not None and spx <= -1.0:
        score += 9
        reasons.append("US_EQUITY_WEAKNESS")
    if vix is not None and vix >= 12.0:
        score += 15
        reasons.append("VIX_SPIKE")

    macro = _num(data, "macro_event_severity")
    if macro is not None:
        macro = max(0.0, min(100.0, macro))
        if macro >= 85:
            score += 25
            reasons.append("MACRO_EVENT_EXTREME")
        elif macro >= 70:
            score += 15
            reasons.append("MACRO_EVENT_HIGH")

    score = max(0, min(100, int(round(score))))
    if score >= 85:
        return MarketRiskAssessment(score, "EXTREME", True, 0.0, tuple(reasons))
    if score >= 65:
        return MarketRiskAssessment(score, "HIGH", False, 0.50, tuple(reasons))
    if score >= 45:
        return MarketRiskAssessment(score, "ELEVATED", False, 0.75, tuple(reasons))
    if score >= 25:
        return MarketRiskAssessment(score, "WATCH", False, 1.0, tuple(reasons))
    return MarketRiskAssessment(score, "NORMAL", False, 1.0, tuple(reasons))


def compact_risk_log(assessment: MarketRiskAssessment) -> str:
    reasons = ",".join(assessment.reasons[:8]) or "NONE"
    return (
        "[MARKET_RISK_INTELLIGENCE] "
        f"score={assessment.score} level={assessment.level} "
        f"block_new_entries={assessment.block_new_entries} "
        f"size_multiplier={assessment.size_multiplier:.2f} reasons={reasons}"
    )
