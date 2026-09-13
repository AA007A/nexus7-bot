# NEXUS real OOS replay methodology

This research path is analytics-only and never sends, changes, or cancels exchange orders.

## Evidence rules

- Only candles closed at the historical decision timestamp are visible to strategy/NEXUS.
- NEXUS freshness checks run against a frozen historical clock, not wall-clock time.
- Every baseline-eligible candidate receives a forward simulated outcome, including candidates rejected by NEXUS.
- Same-bar stop/target ambiguity is resolved conservatively: stop first.
- Market fills include adverse slippage; round-trip fees are charged; public KuCoin funding settlements are included when available.
- Outcomes are normalized to net R using the candidate's original stop risk.
- The edge gate compares baseline expectancy against NEXUS-approved expectancy and requires sufficient approved/rejected samples plus a positive bootstrap 95% lower bound.

## Historical-context limitation

The public replay currently has candles, a decision-time ticker proxy, and public funding history, but not point-in-time historical order book or account-quality open-interest context matching production. Therefore `historical_context.parity_complete=false` and the final status remains `AI_EDGE_NOT_PROVEN` even if the statistical uplift gate passes. This prevents a research dataset with weaker context than production from promoting the AI.

The replay is still useful for measuring candidate selection behavior and for identifying whether NEXUS filtering appears directionally beneficial before full-context evidence is available.
