"""Read-only event-intelligence observability for NEXUS-7 news inputs.

Transforms already-fetched headlines into structured event telemetry. It does
not change NEXUS scores, thresholds, sizing, or execution permissions.
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Iterable, Mapping, Any

from bot.event_intelligence import aggregate_events, classify_event


def build_event_snapshot(headlines: Iterable[Mapping[str, Any]]) -> dict:
    events = []
    for item in headlines:
        if not isinstance(item, Mapping):
            continue
        title = str(item.get("title", "") or "").strip()
        source = str(item.get("source", "") or "").strip()
        if not title:
            continue
        events.append(classify_event(title, source))

    aggregate = aggregate_events(events)
    return {
        "event_count": len(events),
        "aggregate": aggregate,
        "events": [asdict(e) | {"event_type": e.event_type.value} for e in events],
        "decision_effect": "NONE",
        "execution_effect": "NONE",
    }


def compact_event_log(snapshot: Mapping[str, Any]) -> str:
    """Stable, non-secret one-line representation for runtime logs."""
    aggregate = snapshot.get("aggregate", {}) if isinstance(snapshot, Mapping) else {}
    types = ",".join(aggregate.get("types", []) or []) or "NONE"
    assets = ",".join(aggregate.get("assets", []) or []) or "NONE"
    return (
        "[NEWS_EVENT_INTELLIGENCE] "
        f"events={int(snapshot.get('event_count', 0) or 0)} "
        f"severity={int(aggregate.get('severity', 0) or 0)} "
        f"direction={float(aggregate.get('direction', 0.0) or 0.0):+.3f} "
        f"sources={int(aggregate.get('consensus_sources', 0) or 0)} "
        f"types={types} assets={assets} "
        "decision_effect=NONE execution_effect=NONE"
    )
