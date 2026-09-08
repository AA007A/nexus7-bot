# NEXUS-7 Professional Hardening Roadmap — 2026-09-08

This roadmap converts the latest adversarial audit into staged engineering work without enabling live exchange mutation during validation.

## Phase A — Risk foundation

1. Replace buying-power sizing with stop-risk sizing:
   - risk budget = equity × risk_pct
   - effective loss includes stop distance + fees + conservative slippage
   - leverage constrains collateral; it does not define market-risk budget
   - quantity is always rounded down to exchange step
   - minimum order never overrides the risk or collateral budget

2. Separate capital semantics permanently:
   - equity: used for drawdown and risk budget
   - available_collateral: used for affordability
   - position_margin: already committed to positions
   - order_margin: reserved by active orders
   - unrealized PnL: reported separately

New pure module: `bot/professional_risk.py`.

## Phase B — Final pre-dispatch gates

Before any future exchange order submission, run a fresh read-only recheck:

- current exchange positions
- current active exchange orders
- top-of-book spread
- signal-to-executable-price drift
- visible book depth vs requested order size

Any read failure is fail-closed. Existing same-symbol exposure or any active exchange order blocks the candidate until reconciled.

New module: `bot/pre_dispatch_guard.py`.

## Phase C — Controlled E2E validation

A future controlled E2E is only valid when the following evidence exists in order:

1. PRE_DISPATCH_EXPOSURE_CLEAR
2. MICROSTRUCTURE_PASS
3. DURABLE_INTENT_PERSISTED
4. ORDER_ACK
5. FILL_CONFIRMED
6. PROTECTION_CONFIRMED
7. POSITION_RECONCILED
8. EXIT_CONFIRMED
9. FINAL_EXPOSURE_CLEAR

`bot/e2e_validation_contract.py` validates the evidence transcript but never mutates the exchange.

## Phase D — NEXUS confidence calibration and OOS

Raw heuristic confidence must not be treated as an empirical win probability until calibrated.

Required metrics:

- Brier score
- empirical win rate by confidence bucket
- expectancy in R by bucket
- sample count per bucket
- chronological purged walk-forward windows
- untouched OOS performance

New module: `bot/nexus_calibration.py`.

## Phase E — Overfitting controls

No parameter promotion based on the same observations used for selection.

Mandatory promotion chain:

`development -> purged validation -> untouched OOS -> forward PAPER/SHADOW`

Keep optimization windows chronological. Do not random-shuffle time series. Persist the exact parameter set and data period used in each experiment.

## Phase F — Event intelligence

Replace single-headline keyword sentiment as the only semantic layer with structured events:

- CPI/inflation
- FOMC/Fed
- interest rates/yields
- ETF
- hacks/exploits
- regulation/enforcement
- insolvency/default
- war/geopolitics
- token-specific events

Aggregate independent sources and preserve source count, severity, direction and affected assets.

New module: `bot/event_intelligence.py`.

## Phase G — Core integration and sitecustomize consolidation

The modules in this branch are deliberately pure and isolated first. The next integration PR should move safety invariants into explicit engine/core calls rather than adding more monkey patches.

Target architecture:

- `TradingEngine._open` explicitly calls capital snapshot / stop-risk sizing
- explicit pre-dispatch exposure + microstructure gate immediately before durable intent + dispatch
- RiskManager stores equity and available collateral separately
- news reader produces structured event context consumed by NEXUS
- confidence reporting distinguishes raw score from calibrated probability
- `sitecustomize.py` retains startup compatibility only and stops being the primary owner of business-risk invariants

## Current safety boundary

This roadmap and the new modules do not alter Railway variables, do not release `VALIDATION_LOCK`, and do not place/cancel/modify exchange orders. Production must remain `execution_effect=NONE` until later validation criteria are satisfied.
