"""Deterministic market-risk intelligence for NEXUS-7.

Normalizes derivatives, cross-asset and macro observations into a conservative
risk score. It never authorizes an order. Missing data is fail-neutral.
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
    """Return 0..100 dislocation risk; never an entry authorization.

    Cross-asset inputs use percentage changes over the collector window:
    SPX, Nasdaq-100, VIX, DXY, US 2Y and US 10Y yields. Macro-event severity
    covers FOMC/rates, CPI/PCE/PPI, jobs/unemployment, GDP/recession and other
    structured US macro news. Signals are combined conservatively.
    """
    data = signals or {}
    score = 0
    reasons: list[str] = []

    liq = _num(data, "liquidation_usd_1h")
    if liq is not None and liq >= 500_000_000:
        score += 30; reasons.append("LIQUIDATION_CASCADE_EXTREME")
    elif liq is not None and liq >= 150_000_000:
        score += 18; reasons.append("LIQUIDATION_CASCADE_HIGH")

    oi = _num(data, "open_interest_change_pct")
    funding = _num(data, "funding_rate_pct")
    if oi is not None and abs(oi) >= 12.0:
        score += 15; reasons.append("OPEN_INTEREST_DISLOCATION")
    elif oi is not None and abs(oi) >= 7.0:
        score += 8; reasons.append("OPEN_INTEREST_ELEVATED")
    if funding is not None and abs(funding) >= 0.08:
        score += 12; reasons.append("FUNDING_EXTREME")

    spx = _num(data, "spx_change_pct")
    ndx = _num(data, "ndx_change_pct")
    vix = _num(data, "vix_change_pct")
    dxy = _num(data, "dxy_change_pct")
    us2y = _num(data, "us2y_yield_change_bps")
    us10y = _num(data, "us10y_yield_change_bps")

    if spx is not None and spx <= -2.0:
        score += 15; reasons.append("SPX_RISK_OFF")
    elif spx is not None and spx <= -1.0:
        score += 7; reasons.append("SPX_WEAKNESS")
    if ndx is not None and ndx <= -2.5:
        score += 15; reasons.append("NDX_RISK_OFF")
    elif ndx is not None and ndx <= -1.25:
        score += 7; reasons.append("NDX_WEAKNESS")
    if vix is not None and vix >= 12.0:
        score += 15; reasons.append("VIX_SPIKE")
    elif vix is not None and vix >= 7.0:
        score += 7; reasons.append("VIX_RISING")
    if dxy is not None and dxy >= 1.0:
        score += 12; reasons.append("DXY_SURGE")
    elif dxy is not None and dxy >= 0.5:
        score += 6; reasons.append("DXY_RISING")
    if us2y is not None and us2y >= 15.0:
        score += 10; reasons.append("US2Y_YIELD_SHOCK")
    elif us2y is not None and us2y >= 8.0:
        score += 5; reasons.append("US2Y_YIELD_RISING")
    if us10y is not None and us10y >= 15.0:
        score += 10; reasons.append("US10Y_YIELD_SHOCK")
    elif us10y is not None and us10y >= 8.0:
        score += 5; reasons.append("US10Y_YIELD_RISING")

    macro = _num(data, "macro_event_severity")
    if macro is not None:
        macro = max(0.0, min(100.0, macro))
        if macro >= 85:
            score += 25; reasons.append("MACRO_EVENT_EXTREME")
        elif macro >= 70:
            score += 15; reasons.append("MACRO_EVENT_HIGH")

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
