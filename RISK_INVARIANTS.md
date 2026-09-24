# RISK_INVARIANTS — single source of truth

Every risk concept has one owner. Any other module that reads the concept must call the owner and must not re-derive it. `bot/risk_policy.py` is pure: it does no I/O and never mutates anything.

| Concept | Definition | Single source of truth | Notes |
|---|---|---|---|
| **Equity** | KuCoin `accountEquity` (includes unrealized PnL) from an authenticated `account-overview` read | `CapitalState.equity` from `professional_risk.capital_state_from_account_overview`, held by `RiskManagerV3` (`engine.risk.professional_snapshot.capital`) | Never `availableBalance`. PAPER uses the virtual wallet balance. |
| **Available collateral** | `availableMargin` (CROSS-aware), else `availableBalance` | `CapitalState.available_collateral`. For LIVE sizing it is `min(snapshot, fresh _pilot_available_balance)` | A later, lower read always wins (`_reconcile_latest_available`). |
| **Peak equity (HWM)** | Durable, cash-flow-adjusted high-water mark of equity | `drawdown_persistence.restore_update_real_account_peak` → `RiskManagerV3._peak_equity` | Can be lowered only by `rebase_peak_equity` after a verified deposit/withdrawal reconciliation. |
| **Drawdown** | `(peak − equity) / peak` | `RiskManagerV3.drawdown` (legacy `RiskManager.drawdown` is kept in sync for display) | |
| **Drawdown gate** | `drawdown ≥ MAX_DRAWDOWN` ⇒ new position BLOCK | `risk_policy.drawdown_entry_decision` | Independent of `DRAWDOWN_MODE`. `LIVE_RISK_OVERRIDE_APPROVED` has **no effect** on new entries. Invalid or unknown drawdown ⇒ BLOCK. |
| **Override scope** | What an operator override may authorize | `risk_policy.override_may_authorize` | Only CLOSE, REDUCE, CANCEL_EXPOSURE_INCREASING_ORDER, INSTALL_OR_REPAIR_PROTECTION, RECONCILE. |
| **Risk per trade** | `equity × risk_pct`, where `risk_pct = engine._effective_risk_pct() ≤ MAX_RISK_PCT` | `risk_policy.size_new_entry` (`risk_budget`) | `risk_pct > MAX_RISK_PCT` ⇒ BLOCK (not clamped). |
| **Projected stop loss** | `qty × (|entry − stop| + entry·drift + entry·(1+drift)·cost)` | `risk_policy.size_new_entry` (`projected_loss_at_stop`); proven again by `final_loss_budget.validate` at the fresh executable price | `cost = max(2·taker + slippage, per-symbol round-trip model)`. `drift = NEXUS_MAX_SIGNAL_DRIFT_BPS / 1e4` (the worst fill the pre-dispatch guard accepts). |
| **Margin cap** | `available × min(MAX_MARGIN_PCT, OPERATOR_MARGIN_CAP_PCT) × leverage / entry` | `risk_policy.size_new_entry` caps `margin` and `operator` | A **cap**, never a utilization target. `OPERATOR_MARGIN_CAP_PCT` defaults to 0.50. |
| **Liquidation cap** | `equity − qty·loss_per_unit ≥ 2 × mmr × qty × entry` | `risk_policy.size_new_entry` cap `liquidation` | `mmr` comes from instrument metadata; if unknown, a conservative 5% is used (this can only shrink quantity). |
| **Portfolio cap** | `Σ open projected stop losses + candidate ≤ MAX_POSITIONS × equity × MAX_RISK_PCT` | `risk_policy.size_new_entry` cap `portfolio`; `risk_policy.projected_open_risk` | Open positions whose geometry is unknown consume a full per-trade budget. `n_open ≥ MAX_POSITIONS` ⇒ BLOCK. |
| **CROSS stress** | All bot positions plus the candidate at their stops at the same time: `(maintenance + closing fees) / (stressed margin − opening fee − slippage) < MAX_STOP_STRESS_RISK_RATE` | `cross_portfolio_stress.evaluate` with threshold from `risk_policy` (default 0.50, ceiling 0.90) | Unknown MMR, stale requirement, missing protection, local/exchange mismatch, or extra non-reduce orders ⇒ BLOCK. |
| **Daily stop** | `min(balance × DAILY_STOP_LOSS_PCT, DAILY_STOP_LOSS if > 0)` | `risk_policy.effective_daily_stop_limit` | An absolute value can only tighten the limit. Invalid absolute ⇒ ignored (the percentage governs). Balance ≤ 0 ⇒ error (fail closed). |
| **Leverage** | `cfg.LEVERAGE`, never mutated at runtime | `RiskPolicy.leverage` | Enters collateral (margin) and liquidation only. It never enters the loss budget. |
| **Quantity** | `floor_lot(min(RiskManagerV3 adapter qty, size_new_entry qty))` | LIVE: `final_sizing_invariants.size_pilot_entry`. PAPER/core: `ProfessionalRiskAdapter.size` → `size_new_entry` | If below the exchange minimum ⇒ 0 (NO TRADE). Never escalated. |
| **Configuration validity** | Bounds + contradictions | `RiskPolicy.violations()` | Any violation ⇒ new entries BLOCK (`[RISK_POLICY_INVALID]`); existing positions stay managed. |

## Configuration bounds (violations, never silently clamped)

| Variable | Valid range |
|---|---|
| `LEVERAGE` | [1, 125] |
| `MAX_RISK_PCT` | (0, 0.02] |
| `POST_TARGET_RISK` | (0, MAX_RISK_PCT] |
| `MAX_MARGIN_PCT`, `OPERATOR_MARGIN_CAP_PCT` | (0, 1] |
| `MAX_DRAWDOWN` | (0, 1) |
| `MAX_POSITIONS` | int in [1, 10] |
| `DAILY_STOP_LOSS_PCT` | (0, 0.20] |
| `DAILY_STOP_LOSS` | finite, ≥ 0 (0 = disabled) |
| `MIN_RR_RATIO` | > 0 |
| `MAX_STOP_STRESS_RISK_RATE` | (0, 0.90] |
| `NEXUS_EXPECTED_SLIPPAGE_PCT` | [0, 0.05) |
| Contradiction | `MAX_RISK_PCT < DAILY_STOP_LOSS_PCT` and `MAX_RISK_PCT < MAX_DRAWDOWN` |

Non-authoritative variables. Their presence is reported in `ignored_overrides` and never honored: `LIVE_RISK_OVERRIDE_APPROVED`, `DAILY_STOP_OPERATOR_OVERRIDE`. `DRAWDOWN_MODE` is telemetry-only.

## Worked example: production state (equity = available = 25.9007 USDT, 50x, MAX_RISK_PCT = 1%)

SOL-like contract (multiplier 0.01), entry 150, cost 0.22%, drift allowance 0.20%:

| Stop distance | Old qty (50% margin @50x) | Old loss at stop | New qty | New projected loss | New margin | Binding |
|---|---|---|---|---|---|---|
| 0.4% | 4.31 | 4.008 USDT (**15.5%** equity) | 0.21 | 0.2584 (1.00%) | 0.63 | RISK |
| 1.0% | 4.31 | 7.887 (30.5%, blocked only by old 25% limit) | 0.12 | 0.2557 (0.99%) | 0.36 | RISK |
| 2.0% | 4.31 | 14.352 (55.4%, blocked by old limit) | 0.07 | 0.2541 (0.98%) | 0.21 | RISK |

The old final loss budget allowed up to `margin × 0.5 = 6.48 USDT = 25%` of equity per trade. The new budget is 0.259 USDT.

Daily stop: `min(25.9007 × 3% = 0.78, 100) = 0.78 USDT`. The baseline produced 100 USDT.

Drawdown: `(63.7943 − 25.9007) / 63.7943 = 59.40% ≥ 50%` ⇒ **new entries BLOCKED**. This holds whatever `LIVE_RISK_OVERRIDE_APPROVED` is set to.
