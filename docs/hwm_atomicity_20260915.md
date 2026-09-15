# HWM atomicity hardening — 2026-09-15

The LIVE account-equity HWM and its provenance snapshot are one logical durable-state transition.

## Invariant

A transition must commit both `risk:account_equity_peak:v1` and `risk:account_equity_peak:provenance:v1`, or commit neither.

PostgreSQL uses one database transaction. SQLite uses `BEGIN IMMEDIATE` with explicit commit/rollback. Strict failures raise `PersistenceError` before the in-process HWM cache/risk state is advanced.

This change has no execution-authority effect and does not change LIVE mode, KuCoin routing, leverage, operator sizing, NEXUS strategy, or RiskManager thresholds.
