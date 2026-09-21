# BGX Drawdown Policy Audit — 2026-09-21

Scope: audit only. No runtime behavior, thresholds, sizing, leverage, strategy,
NEXUS, CROSS, SL/TP, order routing or execution permissions are changed by
this document.

## Executive finding

The effective LIVE execution policy is **hard gate by default with an explicit
environment override**. The final execution authority is
`bot/operator_runtime_policy.py`, not `cfg.DRAWDOWN_MODE`.

At the audited production SHA
`87d4fe83871bb2be277cf5e03909fa61821736b1`, production telemetry proves the
override is active while drawdown is above the configured limit:

- durable equity: approximately 22.4836 USDT;
- durable peak equity: approximately 63.7943 USDT;
- drawdown: approximately 64.76%;
- configured maximum drawdown: 50.00%;
- `override=true`;
- `entries_blocked=false` at the drawdown gate.

Other independent readiness, ownership, balance, capacity, protection,
reconciliation and execution gates remain in force.

## Canonical policy source

`bot/operator_runtime_policy.py` is installed by the final runtime overlay.
`bot/runtime_contract_guard.py` verifies that the final callable ownership for
`TradingEngine.run`, `TradingEngine._update_balance`,
`RiskManager.can_open` and `RiskManagerV3.can_open` belongs to that policy.

The effective rule is:

```
drawdown < MAX_DRAWDOWN
  -> drawdown gate allows evaluation to continue

drawdown >= MAX_DRAWDOWN AND LIVE_RISK_OVERRIDE_APPROVED != true
  -> block new entries

drawdown >= MAX_DRAWDOWN AND LIVE_RISK_OVERRIDE_APPROVED == true
  -> bypass only the drawdown gate; continue through all other gates
```

## Override activation

The runtime reads exactly:

`LIVE_RISK_OVERRIDE_APPROVED=true`

The comparison is explicit and deterministic after trim/lower normalization.
No API endpoint or in-process mutable override authority was identified in this
audit.

Operationally, the override is activated by changing the Railway service
environment variable.

## Persistence across restart

The drawdown high-water mark is persisted separately in PostgreSQL by
`bot/drawdown_persistence.py`, including provenance for HWM writes and
cash-flow rebases.

The override itself is **not persisted by application code or PostgreSQL**.
It is read from the Railway service environment on every policy evaluation.
Therefore it survives process restarts and redeploys for as long as the Railway
variable remains configured as `true`.

## Auditability

Runtime effect is strongly observable: override use is emitted as CRITICAL
telemetry, including drawdown, configured limit and whether new entries are
blocked.

However, application-level activation provenance is incomplete. The runtime
does not record:

- operator identity that enabled the override;
- activation timestamp as an immutable application audit record;
- approval reason / ticket / incident reference;
- explicit expiry or TTL;
- bounded session/UTC-day scope.

Therefore the current override is explicit and deterministic, but only
partially auditable.

## Expiration

No expiry exists in the canonical policy.

`_risk_override_enabled()` evaluates only the boolean environment value.
There is no timestamp, TTL, UTC day, session ID or automatic retirement
condition tied to `LIVE_RISK_OVERRIDE_APPROVED`.

An enabled override can remain effective indefinitely until the Railway
variable is changed.

## DRAWDOWN_MODE semantic conflict

`bot/config.py` still exposes `DRAWDOWN_MODE` with default `ADVISORY` and
accepts `ADVISORY` / `HARD_GATE`.

The final LIVE entry authority does not use that field to decide whether the
drawdown gate blocks. The final policy is hard gate plus explicit override.

A legacy runtime test also describes an internal pre-overlay balance behavior
as advisory. That does not represent final entry authorization after the
operator runtime overlay and runtime contract are installed.

This is a semantic/configuration debt: two policy descriptions coexist, while
one final authority actually controls execution.

## Is the current ability to enter above max drawdown intentional?

At the code-contract level: **yes**. Tests explicitly require that a true
`LIVE_RISK_OVERRIDE_APPROVED` bypasses the drawdown gate while preserving
other gates.

At the operator-approval provenance level: **not verified**. There is no
application record proving who enabled the current persistent override, why,
or until when it was intended to remain active.

## Desired invariant assessment

`DRAWDOWN_OVERRIDE_MUST_BE_EXPLICIT_AUDITABLE_AND_DETERMINISTIC`

- Explicit: PASS.
- Deterministic: PASS.
- Auditable: PARTIAL.
- Expiring/bounded: FAIL / NOT IMPLEMENTED.
- Restart semantics: deterministic, because Railway configuration persists.

Overall audit status: **PARTIAL**.

No behavioral remediation is included here. Any change to override semantics,
expiry, authorization or drawdown behavior must be a separate policy decision
and PR.
