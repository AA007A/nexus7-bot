# NEWS event runtime observability

This cut wires the existing structured event-intelligence classifier into the active public RSS refresh loop as telemetry only.

The runtime collects all fresh, relevant, deduplicated headlines from CoinDesk, CoinTelegraph, The Block and Decrypt, classifies them into structured event types, aggregates source consensus/severity/direction/assets, and emits a compact `[NEWS_EVENT_INTELLIGENCE]` log line.

The existing single-headline sentiment path remains unchanged and continues to be the only news contribution currently added to market sentiment. Structured event telemetry has `decision_effect=NONE` and `execution_effect=NONE`; it does not alter NEXUS scores, thresholds, sizing, RiskManagerV3, pilot gates, release state or exchange routing.

A failure in structured event telemetry is caught and logged after the normal headline cache update so it cannot suppress or change the existing news context behavior.
