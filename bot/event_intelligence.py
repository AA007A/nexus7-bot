"""Structured event intelligence for crypto/macro headlines.

This is a deterministic normalization/aggregation layer. It upgrades raw
keyword sentiment into typed events with severity, scope and source consensus.
It does not call an exchange and does not authorize execution.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
from typing import Iterable


class EventType(str, Enum):
    MACRO_CPI = "MACRO_CPI"
    MACRO_FOMC = "MACRO_FOMC"
    RATES = "RATES"
    ETF = "ETF"
    HACK_EXPLOIT = "HACK_EXPLOIT"
    REGULATION = "REGULATION"
    INSOLVENCY = "INSOLVENCY"
    WAR_GEOPOLITICS = "WAR_GEOPOLITICS"
    TOKEN_SPECIFIC = "TOKEN_SPECIFIC"
    OTHER = "OTHER"


@dataclass(frozen=True)
class HeadlineEvent:
    title: str
    source: str
    event_type: EventType
    severity: int  # 0..100
    direction: int  # -1 bearish, 0 neutral/uncertain, +1 bullish
    assets: tuple[str, ...] = ()


_PATTERNS = [
    (EventType.MACRO_CPI, re.compile(r"\b(cpi|consumer price|inflation data)\b", re.I)),
    (EventType.MACRO_FOMC, re.compile(r"\b(fomc|federal reserve|\bfed\b|powell)\b", re.I)),
    (EventType.RATES, re.compile(r"\b(rate hike|rate cut|interest rate|yield)\b", re.I)),
    (EventType.ETF, re.compile(r"\b(etf|exchange-traded fund|blackrock|fidelity)\b", re.I)),
    (EventType.HACK_EXPLOIT, re.compile(r"\b(hack|exploit|breach|drain|stolen funds)\b", re.I)),
    (EventType.REGULATION, re.compile(r"\b(sec|cftc|regulation|ban|lawsuit|enforcement)\b", re.I)),
    (EventType.INSOLVENCY, re.compile(r"\b(bankrupt|bankruptcy|insolven|default|liquidation filing)\b", re.I)),
    (EventType.WAR_GEOPOLITICS, re.compile(r"\b(war|missile|invasion|sanction|geopolit)\b", re.I)),
]

_BULL = re.compile(r"\b(approve|approved|cut rates|inflow|adoption|launch|surge|record high)\b", re.I)
_BEAR = re.compile(r"\b(reject|rejected|hack|exploit|ban|lawsuit|hike rates|outflow|default|bankrupt|war)\b", re.I)
_ASSETS = ("BTC", "ETH", "SOL", "XRP", "ADA", "DOGE", "LINK", "AVAX", "DOT", "LTC", "NEAR", "ATOM")


def classify_event(title: str, source: str) -> HeadlineEvent:
    text = str(title or "").strip()
    event_type = EventType.OTHER
    for et, pat in _PATTERNS:
        if pat.search(text):
            event_type = et
            break
    assets = tuple(a for a in _ASSETS if re.search(rf"\b{re.escape(a)}\b", text, re.I))
    if event_type is EventType.OTHER and assets:
        event_type = EventType.TOKEN_SPECIFIC

    bull = bool(_BULL.search(text))
    bear = bool(_BEAR.search(text))
    direction = 1 if bull and not bear else -1 if bear and not bull else 0

    base = {
        EventType.HACK_EXPLOIT: 90,
        EventType.INSOLVENCY: 90,
        EventType.WAR_GEOPOLITICS: 85,
        EventType.MACRO_FOMC: 80,
        EventType.MACRO_CPI: 80,
        EventType.RATES: 75,
        EventType.REGULATION: 70,
        EventType.ETF: 65,
        EventType.TOKEN_SPECIFIC: 55,
        EventType.OTHER: 25,
    }[event_type]
    if direction:
        base = min(100, base + 5)
    return HeadlineEvent(text, source, event_type, base, direction, assets)


def aggregate_events(events: Iterable[HeadlineEvent]) -> dict:
    rows = list(events)
    if not rows:
        return {"severity": 0, "direction": 0.0, "consensus_sources": 0, "types": []}
    unique_sources = {e.source for e in rows if e.source}
    weighted = sum(e.direction * e.severity for e in rows)
    denom = sum(e.severity for e in rows) or 1
    severity = max(e.severity for e in rows)
    # Independent source agreement increases urgency but never fabricates direction.
    if len(unique_sources) >= 3:
        severity = min(100, severity + 10)
    return {
        "severity": severity,
        "direction": round(weighted / denom, 3),
        "consensus_sources": len(unique_sources),
        "types": sorted({e.event_type.value for e in rows}),
        "assets": sorted({a for e in rows for a in e.assets}),
    }
