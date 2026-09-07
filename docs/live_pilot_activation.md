# NEXUS-7 controlled LIVE pilot activation

This checklist prepares the first real-money pilot without activating it.

## Safety invariants before release

- Latest `main` Quality Check must be green on the exact Railway deployment SHA.
- `VALIDATION_LOCK` remains installed until a separate explicit release decision.
- `REAL_TRADING_PILOT=true` must be configured for the pilot session.
- `PILOT_ACCOUNT_CONFIRMED=true` must confirm the intended KuCoin Futures account.
- `PAPER_TRADE=false` and `LIVE_TRADING_CONFIRMED=I_UNDERSTAND_THE_RISK` are still required by the exchange client.
- `PILOT_RELEASE_APPROVED=I_APPROVE_ONE_LIVE_PILOT_ORDER` is an additional independent release gate.
- Pilot limits remain one concurrent position and one new order submission per process session.
- NEXUS AI approval, RiskManager, IntegrityGuard, fresh market data, instrument metadata, private order reconciliation and durable intent persistence must all pass.
- Any pending/ambiguous order, state divergence, unprotected position or missing private-order registry blocks the pilot.

## Activation sequence

1. Confirm there are no open or unprotected exchange positions and no pending durable orders.
2. Confirm current deployment SHA and post-merge Quality Check success.
3. Confirm read-only authenticated balance/instrument/market-data startup is healthy.
4. Configure the pilot variables only after the above checks pass.
5. Remove the temporary `VALIDATION_LOCK` only in a dedicated reviewed change; do not combine that removal with strategy/risk/config edits.
6. Observe the first order through CREATED/SUBMITTING/SUBMITTED/FILLED, SL/TP protection, private/REST reconciliation and final settlement.
7. After the first submission, the session is consumed. Do not reset the guard to manufacture another order until the full cycle is reconciled.
8. If any invariant fails, restore PAPER mode and keep the validation lock installed.

No step in this document itself activates LIVE trading.
