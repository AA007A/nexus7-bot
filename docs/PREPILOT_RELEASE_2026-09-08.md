# NEXUS-7 — Pre-Pilot Release Package

Date: 2026-09-08
Baseline SHA: `90e90db3e452897f28647e5b5b1540756a4377fa`
Railway project/service: `BGX CAPITAL / nexus7-bot`
Validated deployment: `34332980-cf79-411c-ba19-df2043eb84c8`
Classification: **GO for pre-pilot code freeze; NOT an authorization for real-money execution.**

## 1. Freeze boundary

This package freezes the validated pre-pilot baseline. Any code, dependency, environment-variable, execution-gate, risk, sizing, exchange-adapter, database, startup, or observability change after the baseline invalidates this release evidence until Quality Check and runtime validation are repeated.

`VALIDATION_LOCK` remains part of the validated state. This document must not be interpreted as permission to remove it, enable exchange mutations, or submit a real order.

## 2. Validated evidence

- GitHub `main` baseline: `90e90db3e452897f28647e5b5b1540756a4377fa`.
- Quality Check #373: SUCCESS on the baseline merge.
- Railway deployment `34332980-cf79-411c-ba19-df2043eb84c8`: SUCCESS.
- CI deploy gate: PASS after Quality Check success.
- SHADOW runtime: `execution_effect=NONE`.
- Private WebSocket read-only probe: PASS, authenticated and subscription acknowledged.
- Silent-exception audit: `total=3`, all `BEST_EFFORT_LIKELY` and explicitly justified; no REVIEW_HIGH/REVIEW_MEDIUM finding in the validated runtime.
- Protection hardening installed: unverifiable Stop Loss is treated as failure; post-open unprotected pilot state blocks new entries.
- No evidence in this validation package of `execution_effect=SUBMIT` or `execution_effect=MUTATE`.

## 3. Required invariants

A future release assessment is invalid if any invariant below is weakened:

1. New opening requires an explicit NEXUS AI allow result; timeout, exception, invalid result, `None`, or veto fail closed.
2. Reduce-only risk-reduction actions must not depend on NEXUS approval.
3. Durable intent/idempotency must exist before dispatch so ambiguous retries cannot create duplicate openings.
4. Pilot concurrency/session limits remain enforced.
5. Quantity, collateral, market-data freshness, pretrade score and configured leverage gates remain fail closed.
6. Protective levels must be valid relative to entry/liquidation constraints.
7. A Stop Loss that cannot be verified is not considered installed.
8. A newly opened position whose protection cannot be established must enter containment; new entries remain blocked until state is reconciled.
9. Ambiguous exchange responses must be reconciled against exchange state before any retry.
10. Runtime/exchange divergence blocks new exposure.
11. Startup integrity and instrument-readiness checks remain fail closed.
12. No production deploy may bypass the Quality Check gate.

## 4. Abort / NO-GO criteria

Immediately classify the release as NO-GO if any of the following is observed during a future controlled validation:

- CRITICAL/HIGH safety finding without explicit, reviewed justification.
- Quality Check failure or deploy SHA mismatch.
- Authentication/account-mode/instrument metadata inconsistency.
- Duplicate client/order intent or ambiguous retry without reconciliation.
- Position detected without verified protection.
- SL/TP verification failure without containment.
- Exchange position and durable local state disagree materially.
- Risk, liquidation, collateral, quantity or pretrade gate is bypassed.
- NEXUS decision is unavailable but an opening is permitted.
- Unexpected exchange mutation occurs while validation/shadow lock is expected.
- Observability is insufficient to reconstruct candidate → decision → dispatch/containment state.

## 5. Rollback baseline

Known validated rollback reference: `90e90db3e452897f28647e5b5b1540756a4377fa`.

Rollback means restoring the last validated code/configuration combination and re-running Quality Check plus startup/runtime probes. A code rollback alone is not evidence of a safe runtime if environment variables, database state, exchange state, dependencies, or account configuration changed.

If an unexpected live position ever exists, operational containment and reconciliation take precedence over redeploying code. Do not assume redeployment closes or protects exchange positions.

## 6. Evidence required before any future pilot assessment

The release reviewer must capture, for the exact candidate/session:

- exact Git SHA and Railway deployment ID;
- successful Quality Check for that SHA;
- startup integrity/readiness output;
- authenticated private-state connectivity;
- account equity/available collateral read;
- candidate symbol, side, score and NEXUS decision;
- capital-health, sizing, collateral, market-data and pretrade gates;
- quantity validation and configured leverage;
- protective levels and liquidation-guard result;
- durable intent/idempotency reservation;
- exchange acknowledgement/fill reconciliation evidence if a separately authorized test is ever performed;
- verified SL/TP/protection state after any fill;
- local durable state vs exchange-state reconciliation;
- proof that no duplicate opening occurred;
- final audit for CRITICAL/HIGH and unexpected mutation events.

## 7. Change-control rule

After this freeze, do not treat a modified `main` as release-equivalent merely because the service starts. Any change requires a new baseline SHA, a successful Quality Check, a successful Railway deployment, and targeted revalidation of every safety invariant touched by the change.

## 8. Current decision

**PRE-PILOT CODE FREEZE: GO.**

**REAL-MONEY EXECUTION: NOT VALIDATED OR AUTHORIZED BY THIS PACKAGE.**

The purpose of this artifact is to preserve technical evidence and prevent a later release decision from silently weakening the validated safety properties.