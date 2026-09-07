# NEXUS-7 LIVE Release Review

Base reviewed: `f72a37831bcceeb8752e2d9794dc9d55d99c7c43`

## Scope

This document is a release-review artifact only. It does **not** remove or bypass the validation lock, does **not** change Railway variables, and does **not** enable exchange order dispatch.

## Current runtime fact pattern

Railway is configured such that the KuCoin client resolves the trading mode as LIVE, but `bot.validation_safety_lock` is installed by `sitecustomize.py` and fails closed before any exchange-facing TradingEngine connection is allowed.

## Lock interception map

`bot/validation_safety_lock.py` wraps these `TradingEngine` methods when `paper_trade` is false:

- `_connect` -> sets `active=False`, `connected=False`, logs a CRITICAL block, returns `False`.
- `_open` -> logs that real-order opening is blocked and returns `None`.
- `_sync_positions` -> returns `None`.
- `_reconcile_exchange_positions` -> returns an empty list when the method exists.
- `_guard_naked_positions` -> returns `None` when the method exists.

The lock is installed from `sitecustomize.py` through `_validation_safety_lock.install(_log)`.

## Consequence

The current LIVE configuration cannot progress to private exchange reconciliation, position synchronization, naked-position guarding, or order opening through the engine while the lock remains installed. The repeated runtime message `LIVE runtime blocked: PAPER validation is not complete` is expected behavior, not a crash.

## Release decision requirements

Any future LIVE release should be treated as a separate controlled change. Before that decision, all of the following must remain true:

1. Exact production SHA has a successful Quality Check and deploy gate.
2. PRE-LIVE read-only reconciliation succeeds against the production account.
3. No open real position, active order, ambiguous durable order, or unprotected position exists at release time.
4. Private authenticated order channel/reconciliation is healthy.
5. The NEXUS AI execution gate remains fail-closed (`execution_allowed is True` is required for new exposure).
6. Pilot constraints remain in force: one concurrent position and one new submission per process session.
7. Explicit pilot release authorization remains independent from the generic LIVE acknowledgement.
8. Daily-stop, drawdown, liquidation, idempotency and durable-order protections remain unchanged.
9. No release change may alter risk parameters, leverage, strategy thresholds, credentials, or news logic as a side effect.
10. Rollback must be immediate: restoring the validation-lock install must return the engine to fail-closed LIVE behavior.

## Release-review conclusion

`CODE_READINESS = PASS`

`RUNTIME_LIVE_CONFIGURATION = PRESENT`

`VALIDATION_LOCK = ACTIVE`

`REAL_ENGINE_CONNECTION = BLOCKED`

`REAL_ORDER_OPENING = BLOCKED`

`RELEASE_ACTION = NOT PERFORMED`

The remaining transition is not a bug fix. It is an explicit operational release decision that changes the system from incapable of exchange-facing LIVE execution to capable of it. This review intentionally leaves that transition unperformed.
