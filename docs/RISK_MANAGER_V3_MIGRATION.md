# RiskManagerV3 migration boundary

RiskManagerV3 is the replacement target for the legacy buying-power sizing path.

## Intended engine migration

1. Obtain a fresh authenticated account overview before sizing.
2. Normalize it into `CapitalState`.
3. Update `RiskManagerV3` with the fresh capital snapshot.
4. Compute size from `equity * risk_pct / effective stop loss` using the planned stop.
5. Constrain required margin by `available_collateral * MAX_MARGIN_PCT`.
6. Apply microstructure gates and a final read-only exchange exposure recheck immediately before durable dispatch intent.
7. Preserve NEXUS AI fail-closed approval and all existing durable/idempotency/protection gates.

## Non-goals of this change

- It does not enable LIVE trading.
- It does not remove VALIDATION_LOCK.
- It does not modify Railway variables.
- It does not place, cancel, amend, protect, or close an exchange order.
- It does not classify controlled exchange E2E as validated.

The legacy `RiskManager.size()` remains in place until the engine integration PR can replace its call site with regression evidence.