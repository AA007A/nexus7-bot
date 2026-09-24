# AUDIT_10_10 — NEXUS-7 / BGX CAPITAL production hardening audit

- Baseline SHA: `77ae453e3d01dc269c3b1ee3a4225900c22d9634` (confirmed with `git rev-parse HEAD`)
- Branch: `claude/nexus7-production-hardening-5aq2sc`
- Scope of this pass: P0 risk authority (drawdown, sizing, loss budget, daily stop, CROSS stress), configuration contract, provider health, silent exceptions, CI decision-path coverage.
- Nothing here was deployed. No Railway variable was read or changed. No order was sent.

"10/10" here grades engineering, risk control and evidence quality. It says nothing about expected returns.

---

## 1. Execution path: signal to protected position

```
KuCoinClient WS/REST ─► market_data_integrity (closed-candle, stale fail-closed)
  ─► Analyzer.analyze (strategy.py) ─► adaptive_mtf_entry ─► pullback_confirmation_hardening
  ─► TradingEngine._scan_all_and_enter            [gated by: active, daily stop (durable_daily_stop),
                                                   daily PnL checkpoint, risk.can_open (drawdown +
                                                   policy validity + MAX_POSITIONS), IntegrityGuard,
                                                   viable_symbols]
  ─► TradingEngine._open(sig)                        (engine.py:2666)
       ├─ durable.can_open                            durable state confirmed
       ├─ NexusRuntimeEngine._nexus_validate          NEXUS ensemble decision
       │    └─ _prepare_professional_risk             fresh account capital, HWM, V3.can_open, set_plan(entry, SL, risk_pct)
       ├─ _refresh_entry_balance (pre-sizing)
       ├─ PilotGuard.can_open_pilot                    checks 1..14 incl. 9B drawdown (now DRAWDOWN_MODE-independent)
       ├─ engine.minimum_base_quantity(info, entry)    ◄ FINAL SIZING AUTHORITY (final_sizing_invariants)
       │    = floor_lot(min(RiskManagerV3 adapter qty, risk_policy.size_new_entry qty))
       ├─ pre-trade score (score.py)
       ├─ _refresh_entry_balance (final)               ◄ cross_portfolio_stress (2nd position) ─► pilot_risk_cap
       │                                                 fresh spread/depth/drift + equity loss-budget recheck
       ├─ order registry + durable intent (before network I/O)
       └─ KuCoinClient.place_order ─► live_execution_fence (ownership lease/fencing token)
              ─► pilot_submission_counter ─► kucoin_native_tpsl (protected entry, readback verify)
              ─► kucoin_cross_margin_order ─► fill normalization ─► wait_for_fill (visibility race)
  ─► post-open protection verification (prelive_protection_failclosed) ─► native stop repair
  ─► position management loop: _guard_naked_positions, _sync_positions, stagnation/invalidation,
     partial TP (durable_partial_exit), trailing, RR exit ─► reconciliation (durable_reconcile_hardening)
```

## 2. Position-sizing authorities (before → after)

| Layer (install order) | Before | After |
|---|---|---|
| `bot/quantity.minimum_base_quantity` | exchange minimum lot | unchanged (used only outside LIVE pilot) |
| `pilot_live_runtime` | 50% available as position notional (preliminary) | unchanged, inner, superseded |
| `pilot_risk_cap_hardening` | `min(target, risk)` | unchanged, inner, superseded; its fresh pre-dispatch recheck now uses the **equity** loss budget |
| `operator_runtime_policy._install_margin_sizing` | **50% available as initial margin at 50x = quantity authority** | **removed** |
| `final_sizing_invariants` (outermost) | **OPERATOR_50PCT_EQUITY authority; RiskManagerV3 "VALIDATION_GATE" only** | `floor_lot(min(RiskManagerV3 qty, risk_policy.size_new_entry qty))` |
| `ProfessionalRiskAdapter.size` (PAPER + LIVE) | `stop_risk_size` (risk + margin) | `risk_policy.size_new_entry` (risk, margin, operator cap, liquidation, portfolio, lot) |
| `final_loss_budget.validate` | `limit = margin × 0.50` | `limit = equity × risk_pct` |

## 3. Drawdown / daily-stop authorities (before → after)

| Concept | Writers/readers | After |
|---|---|---|
| Drawdown gate | `RiskManager.can_open`, `RiskManagerV3.can_open` (both patched by `operator_runtime_policy`), `NexusRuntimeEngine._update_balance` (`DRAWDOWN_MODE`), `PilotGuard` 9B, `status_observability` | All delegate to `risk_policy.drawdown_entry_decision`. Override and `DRAWDOWN_MODE` have no authority. |
| Engine `active` restore | `_protect_drawdown_update` set `active=True` under override | Removed. The legacy pause is preserved. |
| Daily stop limit | `engine.py` ×2, `DailyTracker.recalc_limits`, `daily_stop_runtime_hardening._sync_limits`, `nexus_runtime_engine` | All call `risk_policy.effective_daily_stop_limit` (the stricter of the two limits) |
| Daily-stop date override | `durable_daily_stop` (`DAILY_STOP_OVERRIDE_UTC_DAY`) | **Fixed (P1-03, commit `2d92b2f`)**: telemetry only, `override_effect=NONE`; a proven breach always blocks new/increasing exposure |

## 4. Runtime wrapper stack

`runtime_bootstrap.install` makes 53 explicit `.install(...)` calls and `runtime_overlays.install` makes 26. `runtime_contract_guard` (the last one) pins 14 final callables and 8 markers, and refuses startup on drift. The ownership contract is unchanged by this pass. The following were verified by executing `sitecustomize.py` in both SHADOW/PAPER and the production-like controlled-LIVE environment (50x, `LIVE_RISK_OVERRIDE_APPROVED=true`, `DAILY_STOP_LOSS=100`): `RUNTIME_CONTRACT status=PASS`, `sitecustomize status=ok`.

Wrappers that still decide a risk concept after this pass:
- `operator_runtime_policy`: owns `can_open` and `_update_balance`/`run`, and delegates the decision to `risk_policy`.
- `final_sizing_invariants`: owns LIVE quantity, and delegates to `risk_policy`.
- `pilot_risk_cap_hardening` / `pilot_live_runtime`: inner sizing wrappers, now dead weight in the LIVE path (P2-01).

---

## 5. Findings

| ID | Sev | File / function | Root cause | Current (baseline) behavior | Desired invariant | Fix | Tests | Residual risk |
|---|---|---|---|---|---|---|---|---|
| P0-01 | P0 | `operator_runtime_policy._install_drawdown_advisory`, `_protect_drawdown_update` | Operator override implemented as bypass of the hard gate | DD 59.40% ≥ 50% with `LIVE_RISK_OVERRIDE_APPROVED=true` → `can_open=True`, `active` restored, `ALLOW_NEW_ENTRIES` | DD ≥ MAX_DRAWDOWN ⇒ new entry BLOCK. The override may authorize only risk-reducing actions | Gates delegate to `risk_policy.drawdown_entry_decision`. The override is logged `override_effect=NONE`. `active` is never restored | `test_p0_risk_hardening.ScenarioA_*` (9), `test_operator_runtime_policy`, `test_operator_runtime_risk_override`, `test_operator_runtime_policy_contract` | Production is **currently above the limit (59.40%)**. After deploy the bot will open no new positions until the HWM is rebased by a verified cash-flow reconciliation or equity recovers. This is intended. |
| P0-02 | P0 | `nexus_runtime_engine._update_balance`, `pilot.PilotGuard.evaluate` | `DRAWDOWN_MODE` default `ADVISORY` meant the limit never blocked here | Advisory mode: entries allowed over the limit | Same as P0-01 | Gate is independent of `DRAWDOWN_MODE`. The alert reads `HARD_GATE` | `ScenarioA.test_drawdown_mode_advisory_does_not_allow_entries`, `test_nexus_runtime_drawdown_advisory` | `DRAWDOWN_MODE` is now telemetry-only. It should be removed from config in a later cleanup. |
| P0-03 | P0 | `final_sizing_invariants`, `operator_runtime_policy._install_margin_sizing` | Operator 50%-margin figure used as the quantity target; RiskManagerV3 demoted to validation | Qty = 50% available × 50x / price. At equity 25.90 and a 0.4% stop: loss ≈ 4.01 USDT = **15.5% of equity**; old limit allowed up to 25% | final qty = min of all safe caps. Margin is a CAP | Canonical `risk_policy.size_new_entry`, and final = `min(RiskManagerV3, canonical)` | `ScenarioB_*`, `ScenarioC_*`, `test_final_sizing_invariants` (10), `test_professional_risk_adapter` | Sizing is only as good as the SL geometry and the cost model. Gap risk beyond the stop is not bounded. |
| P0-04 | P0 | `final_loss_budget.measure/validate` | Budget expressed as a fraction of entry margin, so leverage scaled the loss budget | `limit = margin × 0.50` ≈ 25% equity at 50x/50% margin | `projected_loss_at_stop ≤ equity × risk_pct` | Equity-based. `equity` is a required keyword | `test_final_loss_budget` (7), `ScenarioB.test_final_loss_budget_is_equity_based` | none known |
| P0-05 | P0 | `daily_stop_runtime_hardening._sync_limits`, `DailyTracker.recalc_limits`, `engine.py` | A positive absolute replaced the percentage | balance 25.90, 3%, abs 100 → stop = **-100 USDT** (386% of balance) | the more restrictive valid limit | `effective_daily_stop_limit` used by every writer | `ScenarioD_*` (7) | Resolved by P1-03 and P1-10. |
| P0-06 | P0 | `risk_policy.size_new_entry` | The minimum lot could be treated as a floor | (baseline pilot path already floored, but not risk-bounded) | min lot > safe qty ⇒ NO TRADE | `MINIMUM_ORDER` block, never escalation | `ScenarioE_*`, `test_final_sizing_invariants.test_minimum_lot_above_budget_is_no_trade` | On a 25.90 USDT account, BTC and wide-stop setups will be skipped. This is correct behavior. |
| P0-07 | P0 | `cross_portfolio_stress` | Hard-coded 0.90 risk-rate ceiling; no slippage on stop fills | 90% of the liquidation point accepted | Configurable conservative threshold; slippage included; fail closed | Policy-owned `MAX_STOP_STRESS_RISK_RATE` (default 0.50, ceiling 0.90) plus slippage | `ScenarioF_*` (7), `test_cross_portfolio_stress` | The risk-rate formula approximates KuCoin CROSS (maintenance / margin balance, liquidation at 100%). The exact endpoint semantics are **not verified against a live account**. The stress runs only for the 2nd position; the 1st is bounded by the per-position liquidation cap. |
| P0-08 | P0 | new `risk_policy.RiskPolicy.violations` | Each module interpreted config independently; no contradiction check | e.g. MAX_RISK_PCT 0.25 accepted silently | Invalid/contradictory policy fails closed with a precise message | `violations()` blocks at `can_open`, in sizing and in the stress gate. `[RISK_POLICY_INVALID]` logged at install | `RiskPolicyConfigurationContract` (9) | Blocks new entries rather than startup, so open positions stay managed (startup block = engine not started). |
| P1-01 | P1 | `sizing_semantics_log_hardening.normalize_record` | LogRecordFactory rewrote log text | Would rewrite truthful "risk-authoritative" lines into "OPERATOR_50PCT_EQUITY" | Logs never misstate authority | Filter inverted: only superseded legacy lines relabelled | `test_sizing_semantics_log_hardening` (5) | none |
| P1-02 | P1 | `status_observability._drawdown_blocked` | Override considered; errors reported "not blocked" | `/status` reported not blocked under override | Status mirrors the gate; unknown ⇒ blocked | Delegates to `risk_policy` | covered by `test_status_*` suites | none |
| P1-03 | P1 | `durable_daily_stop._operator_override_active` | Date-scoped break-glass bypass of a proven daily stop | `DAILY_STOP_OVERRIDE_UTC_DAY=<today>` re-enables entries after a daily-stop breach | Overrides may not authorize new exposure | **Fixed (`2d92b2f`)**: `_operator_override_active` always returns False, never clears `daily_stopped`; notification says the override was IGNORED | `test_daily_stop_operator_override::test_breach_with_every_override_combination_stays_blocked` (+ 4 updated) | none |
| P1-04 | P1 | `market_risk_runtime` | Key presence ≈ "configured"; no health taxonomy | CoinGlass 401 logged, but health was an ad-hoc string | CONFIGURED/AUTHORIZED/FRESH/FAILED/FALLBACK_ACTIVE | `classify_provider_health` + `[MARKET_RISK_PROVIDER_HEALTH]` | `test_market_risk_runtime` (+3) | The CoinGlass entitlement itself is not fixed (it needs an operator key/plan). Binance fallback remains. |
| P1-05 | P1 | `runtime_truth_hooks` (2 × REVIEW_HIGH) | `except Exception: pass` in evidence capture | Capture failures invisible | Telemetry failure isolated but visible | Counted in `CAPTURE_FAILURES` and logged | `RuntimeTruthCaptureFailuresAreVisible` | none |
| P1-06 | P1 | `final_loss_budget.emit_telemetry` | Generic `except Exception: pass` | Silent | Visible | Narrow except, logs `telemetry_error` | `test_observability_only_hardening` | A logging failure now propagates and fails the entry closed (intended). |
| P1-07 | P1 | `.github/workflows/oos_real_replay.yml` | Path filter covered only OOS modules | strategy/NEXUS/config/sizing changes did not trigger replay | Decision-affecting PRs trigger OOS evidence | `bot/decision_affecting_paths.py` + drift test | `test_decision_affecting_paths` (3) | Whether the check is **required** is a branch-protection setting outside the repo. |
| P1-08 | P1 | `nexus_probability.heuristic_win_probability` | Linear map `0.30 + c×0.45` capped at 0.75 | Docstring already says "not an empirically calibrated probability" | Probability calibrated OOS or labelled heuristic | Option A (`cf8afe5`): `nexus_probability_semantics` declares HEURISTIC / uncalibrated / VETO_ONLY; operator text reads "EV heur. … (p não calibrado)". Byte-pinned `nexus_ai.py`/`nexus_probability.py` untouched. Exact-SHA evidence: heuristic Brier 0.261 vs base-rate 0.214, ECE 0.205; fitted slope negative (see OOS_REPORT) | `test_audit_remaining_regressions::HeuristicProbabilityIsLabelled` | EV veto kept (removing it would raise trade frequency without evidence). |
| P1-09 | P1 | Strategy edge | — | OOS replay (2932572, 12 symbols × 180 d): baseline −0.375R, NEXUS-approved −0.324R net (authority CI [−0.496, −0.175]); portfolio replay −49.4% | Positive net expectancy before capital exposure | None. No threshold changes without evidence | — | **Open blocker. See OOS_REPORT.** |
| P1-10 | P1 | `risk_policy.effective_daily_stop_limit` | `round(balance * pct, 2)` display rounding used as the risk value | 25.9007 × 3% = 0.777021 became 0.78 (looser) | internal limit ≤ exact configured limit | Decimal, quantize DOWN to 1e-8, largest float not above; separate floored `display_limit` (`5fabb4f`) | `DailyStopPrecisionIsConservative` (cent-boundary balances 25.9007, 10.01, 1.01, 0.99, 100.005, 1000.005, …) | none |
| P0-09 | P0 | `.github/workflows/oos_real_replay.yml`, `nexus_oos_real_replay.main` | Replay `main()` always returns 0; the "verdict" step only printed | `AI_EDGE_NOT_PROVEN` never failed CI | Decision-affecting PRs fail unless promotion evidence is met | Separate strict gate `python -m bot.nexus_oos_promotion_gate` run as final workflow step (`1af2d5a`); research replay still exits 0 | `test_nexus_oos_promotion_gate` (15), `PromotionGateWorkflowContract` | Branch protection (required checks) is a repository setting outside code. |
| P1-11 | P1 | `nexus_oos_real_replay.replay_symbol` | `getattr(cfg, "NEXUS_MIN_SCORE", 55)` — `cfg` has no such attribute | Replay approved at threshold 55; production uses 60 | Replay uses production threshold | `runtime_nexus_threshold()` = `nexus_ai.MIN_SCORE` (`d548084`) | `test_uses_production_threshold` | none |
| P0-10 | P0 | `nexus_oos_edge_gate`, `nexus_oos_research` | IID row bootstrap on overlapping (10 h horizon), cross-correlated candidates | Uplift IID CI [+0.058, +0.181] looked significant | Dependence-aware CI carries authority | `nexus_oos_inference`: 24 h / 48 h UTC-block bootstrap (all symbols in a block together); authority = most conservative valid interval, requires a block CI (`93631df`) | `BlockBootstrapTests` (3) | Block size is calendar-aligned; lag-1 block autocorrelation reported for review. |
| P0-11 | P0 | `nexus_oos_research.chronological_split` | Row-order split, no purge/embargo | Train outcomes could use validation-period candles | Purge by `outcome_end_ts`, embargo ≥ max(10 h, longest observed horizon) | `purged_split`; threshold research TRAIN→VALIDATION→FINAL TEST once (`93631df`) | `PurgedSplitTests` (3), `ThresholdFinalTestIsolation` | none |
| P0-12 | P0 | OOS evidence | Only a candidate replay existed; overlapping candidates are not tradable positions | No evidence about executable portfolio edge | Event-driven portfolio replay with production constraints; gate needs both layers | `nexus_oos_portfolio_replay` + gate portfolio blockers (`9348413`) | `PortfolioReplayInvariants` (8), portfolio gate tests | Approximations listed in the artifact (event-time circuit-breaker checks, TP1 collateral released at final exit, no discretionary exits). |
| P1-12 | P1 | `nexus_oos_research.performance` | Cumulative candidate-R "drawdown" presented as `max_drawdown_r` | Misreadable as account drawdown | Only portfolio replay reports account drawdown | Renamed `candidate_sequence_drawdown_r` | `CandidateVsPortfolioMetrics` | none |
| P1-13 | P1 | Replay exit parity | Analyzer signals carry `tp1 == tp2` | Exact-SHA replay: `no_partial_tp` and `partial_without_break_even` identical to `current` (−0.2394 R) — partial TP/BE never exercised | Replay exit model matches production exit engine | **Root cause found (`a761d80`)**: `calc_sl_tp` has no caller and `Signal` sets `tp1 = tp2 = tp`, so TP1 == TP2 is production reality. Production's partial does not use `tp1` (fires at 1R + 0.03%). Fixed in the replay by P0-18 | `test_tp1_equals_tp2_in_production_signals` | Remaining exit approximations are listed in the EXIT_PARITY_MATRIX. |
| P1-14 | P1 | `nexus_oos_replay_diagnostics` | Instrumented `decide` would count ~20 research variant calls per candidate | Diagnostics counts corrupted | Diagnostics runs replay without variants | `research=False` (`b777cbb`) | `DiagnosticsDoesNotCountResearchVariants` | none |
| P0-13 | P0 | `nexus_oos_inference.dependence_aware_mean/diff` | Authority = conservative interval over {IID, 24h, 48h} | IID could widen the authority interval, so it had authority | IID diagnostic only | Authority = min lower / max upper over VALID block intervals {24h, 48h, 72h} only (`3fb7757`) | `IidZeroAuthorityInInference` (4) | none |
| P0-14 | P0 | `nexus_oos_promotion_gate.evaluate` | Gate read the legacy IID report (`bootstrap_ci_low_r` → `UPLIFT_CI_NOT_POSITIVE`) and ingested legacy blockers | Two statistical authorities could disagree | One authority | Gate never reads the report; recomputes block-only authority and blocks on inconsistency; legacy IID codes ignored; requires `authority_model = BLOCK_BOOTSTRAP_ONLY_V1` (`3fb7757`) | `IidHasZeroPromotionAuthority` (5) | none |
| P0-15 | P0 | `nexus_oos_real_replay.run_real_replay` | Top-level status came from the legacy IID edge gate | `AI_EDGE_PROVEN` decided by IID | `research_status` (candidate) vs gate verdict | Legacy report moved to `legacy_diagnostic` (`authority: NONE`); canonical blockers only (`a761d80`) | `test_research_sections_present` | none |
| P0-16 | P0 | portfolio replay daily stop | Daily equity anchored at the first candidate of the day | A loss realized after 00:00 but before the first candidate was not counted | Explicit 00:00 UTC reset event | Engine v2 resets at the UTC day boundary; production semantics (realized today + open unrealized, limit from current equity) plus equity-anchored sensitivity (`a761d80`) | `DailyStopMidnightAnchor` (4) | Bar resolution: production checks every ~5 s. |
| P0-17 | P0 | portfolio replay HWM/drawdown | Equity evaluated only at candidate/exit events | Intermediate peaks and troughs invisible to the drawdown gate | Every closed 15m bar | Bar-by-bar marks, HWM, daily PnL, circuit breakers; precedence FUNDING → EXITS → MARK → RESET → ACCOUNT → CANDIDATES → ENTRY (`a761d80`) | `BarByBarDrawdown`, `SameTimestampExitEntry` | Intrabar extremes between closes are not marked. |
| P0-18 | P0 | replay exit model | Native SL/TP shifted by the fill delta; 40-bar (10 h) time exit; TP1 from `sig.tp1` | Production keeps native SL/TP at the UNSHIFTED signal levels, has no time exit, and fires the partial at 1R + 0.03% | Replay = production exit stack | `simulate_production_exit` (native SL/TP unshifted, 1R partial + BE, trailing, 2R exit, STOP_FIRST, gap fills at open) (`a761d80`) | `ProductionExitEmulation` (12) | 5 s polling approximated by bar extremes → APPROXIMATED in the exit matrix. |
| P0-19 | P0 | replay pre-trade path | Post-NEXUS geometry compression, funnel gates and the single-position liquidation rule not modelled | Replay admitted candidates production rejects and kept geometry production changes | Replay reconstructable gates; classify the rest | Funnel (session score, regime direction, expected PnL, drift), `kucoin_contract_risk_hardening` geometry + NEXUS re-run, single-position rule, pilot cap, correlation, cooldown, circuit breaker, exposure capacity (`a761d80`) | `ProductionFactsGuard` (9), pipeline tests | Spread/depth, market-risk feeds, pilot session cap, private CROSS MMR: NOT_REPLAYABLE / LIVE_ONLY ⇒ `PRETRADE_CONTEXT_PARITY_INCOMPLETE`. |
| P0-20 | P0 | replay policy | Portfolio replay inherited CI defaults via `load_policy(cfg)` + env overrides | "Production-configured" label not backed by a pinned input | Pinned, hashed, fail-closed manifest | `research/replay_policy_manifest.json` + `nexus_oos_replay_manifest` (sha256, provenance, runtime verification) (`a761d80`) | `ReplayPolicyManifest` (5) | Production values not in the repo are tagged CODE_DEFAULT or PRODUCTION_REPORTED; Railway was not read. |
| P1-15 | P1 | replay sizing | `size_new_entry` called without the production drift allowance and with realized (not modelled) costs, at the fill price | Replay quantity ≠ production quantity | Identical inputs ⇒ identical quantity | `replay_size_entry` mirrors `size_pilot_entry` (signal entry/stop, `conservative_cost_fraction`, `max_adverse_entry_drift`, lot/minimum, loss budget, margin cap) (`a761d80`) | `SizingParity` (20 snapshot/instrument pairs equal) | Contract specs are CURRENT_CONTRACT_SPEC_PROXY. |
| P1-16 | P1 | portfolio robustness | Trade-level bootstrap of accepted trades presented as portfolio CI | Overstated certainty (path dependence ignored) | Path-dependent inference | Path bootstrap: UTC-block-resampled candidate timelines re-run through the state machine (24/48/72 h); trade-level CI labelled `authority: NONE` (`a761d80`) | `PathBootstrap` (3) | 200 replicates per block length. |
| P1-17 | P1 | production `trailing_safety_hardening.calc_trailing_sl` | `peak_pnl` is not rescaled after the 50% partial | The favourable excursion is read at twice its size; the resulting stop is often rejected by `native_stop_repair` (wrong side of mark) | Consistent quantity scale | **Not changed** (production behaviour; replayed exactly). Candidate for the strategy phase | `test_trailing_uses_unrescaled_peak_after_partial_and_side_check` | Trailing after TP1 behaves differently from its documentation. |
| P1-18 | P1 | production execution capacity | `engine._open` `liquidation.analyze(n_open_positions=len+1)` marks any 2nd position not stop-effective; `ALLOW_SL_BEYOND_LIQUIDATION` is forbidden | LIVE can hold at most ONE position; `MAX_POSITIONS=2` and the CROSS stress gate are unreachable for a 2nd entry | Documented capacity | **Not changed** (safety-conservative). Replayed exactly; CROSS stress classified UNREACHABLE | `test_second_concurrent_position_is_never_liquidation_effective` | Portfolio capacity is lower than configuration suggests. |
| P1-19 | P1 | production pilot session cap | `MAX_NEW_ORDER_SUBMISSIONS_PER_SESSION = 2` per process session | Production submits at most 2 new orders per deploy/restart | — | **Not changed.** NOT_REPLAYABLE (depends on restarts) ⇒ pretrade parity incomplete | `test_parity_is_incomplete_and_names_blockers` | Real trade frequency is bounded by restarts. |
| P1-20 | P1 | production exits | `operator_loss_policy` disables stagnation/CHoCH/regime exits for the LIVE pilot; `min_hold_until` is telemetry only | Earlier docs listed them as active | Accurate exit inventory | Documented in the EXIT_PARITY_MATRIX (INACTIVE_IN_LIVE / TELEMETRY_ONLY) | `test_live_discretionary_exits_disabled` | none |
| P1-21 | P1 | `nexus_oos_real_replay._simulate_net_r` | `_timestamp_index` rebuilt on every call | ~2.7 ms per simulation, dominating replay time | — | Index computed once per symbol (`a761d80`) | existing replay tests | none |
| P2-01 | P2 | `pilot_live_runtime`, `pilot_risk_cap_hardening` sizing hooks | Superseded inner sizing wrappers | Dead in the LIVE path; still installed | One authority | Kept (contract/tests depend on them). Scheduled for removal | — | Maintenance only |
| P2-02 | P2 | Remaining 9 silent handlers | 5 justified best-effort, 4 REVIEW_MEDIUM (runtime_truth ×3, pullback telemetry) | — | classify all | 2 REVIEW_HIGH fixed | selfcheck | REVIEW_MEDIUM remain |
| P2-03 | P2 | `engine.py` (3992 lines) | Monolith | — | Extract stateful authorities with parity tests | Not attempted in this pass (zero-semantic-change refactor requires its own PR) | — | — |

### Required regression scenarios A–J

| | Scenario | Status | Where |
|---|---|---|---|
| A | DD 59.40%, limit 50%, override=true ⇒ BLOCK | new, passing | `tests/test_p0_risk_hardening.py::ScenarioA_*` |
| B | equity 25.9007, 1%, 50x ⇒ loss ≤ budget | new, passing (8×6×6×5×4 grid) | `ScenarioB_*` |
| C | V3 qty < operator qty ⇒ final ≤ risk qty | new, passing | `ScenarioC_*`, `test_final_sizing_invariants` |
| D | 25.9007, 3%, abs 100 ⇒ stricter | new, passing | `ScenarioD_*` |
| E | min contract > risk qty ⇒ NO TRADE | new, passing | `ScenarioE_*` |
| F | stale/missing CROSS data ⇒ BLOCK | new, passing | `ScenarioF_*` |
| G | CoinGlass 401 ⇒ unhealthy + fallback, no signal | new, passing | `test_market_risk_runtime::test_coinglass_401_*` |
| H | restart between submit and ack ⇒ no duplicate | **pre-existing**, passing | `test_durable_execution_restart::test_unresolved_live_intent_blocks_without_resubmission`, `test_exec02_ambiguous_order::test_B2_*`, `test_kucoin_native_tpsl::test_ambiguous_response_recovers_by_client_oid_without_resubmit` |
| I | fill, protection ack delayed ⇒ protect/fail closed | **pre-existing**, passing | `test_prelive_protection_failclosed::*`, `test_kucoin_native_tpsl::test_http_success_without_readback_is_not_verified` |
| J | partial TP + restart ⇒ exchange-authoritative, idempotent | **pre-existing**, passing | `test_durable_partial_exit::*`, `test_exec03_partial_fill_reconcile::test_C6_*`, `test_partial_tp_execution_hardening::test_confirmed_fill_and_be_use_exchange_remaining_quantity` |

## 6. Verification performed

| Check | Result |
|---|---|
| Baseline offline suite at 77ae453 | 1607/1607 pass |
| New P0 tests against baseline code | 14 fail (reproduced), 29 pass |
| Full offline suite (`python -m tests.run_offline`), final code | 1786/1786 pass (1726 before the parity phase) |
| `compileall`, `ruff --select E9,F63,F7,F82`, pyflakes undefined names | pass / pass / 0 |
| `python -m bot.selfcheck` | 0 critical; silent handlers 12 → 9 |
| `python -m bot.release_proof` | `RELEASE_PROOF=PASS` (161/161) |
| CI named safety packs (durable, chaos, TPSL, fill, PAPER/SHADOW E2E) | pass |
| Hardened entrypoint score-drift fail-closed | pass |
| Full bootstrap under controlled-LIVE env (offline) | `RUNTIME_CONTRACT status=PASS` |
| Real OOS replay | Runs in CI only (the sandbox proxy blocks exchange hosts). Historical exact-SHA runs: `b777cbb` 35946377972, `2932572` 35951346282, `70b00b4` 35957755830 (replay OK, strict gate BLOCK each time). Run IDs for later SHAs are recorded in the PR comment and the Actions summary, not committed back (a commit would change the SHA they prove). |

## 7. Status by layer (see RELEASE_READINESS.md)

| Layer | Status |
|---|---|
| ENGINEERING_SAFETY | Met offline: P0/P1 risk fixes, strict CI gate, 1726/1726 tests. PAPER/SHADOW not done. |
| SIGNAL_EDGE | NEGATIVE EXPECTANCY (−0.324 R approved, authority CI < 0). |
| STATISTICAL_CONFIDENCE | NEXUS uplift not significant under dependence-aware inference. |
| PORTFOLIO_EDGE | Negative (−49.4% equity, drawdown-gate halt). |
| CONTEXT_PARITY | Incomplete (OI, order book). |
| REPLAY_PARITY | INCOMPLETE: exit rules are bar-approximated; spread/depth, market-risk feeds, pilot session cap and private CROSS MMR are not replayable (see OOS_REPORT §C). |
| RELEASE_READINESS | PRODUCTION_READY = NO. |
