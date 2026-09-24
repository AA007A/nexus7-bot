# STRATEGY_VALIDATION

This work changed **no strategy threshold, gate, indicator, NEXUS weight or exit rule**. HOLD is a valid decision. All evidence below comes from exact-SHA CI (OOS_REPORT.md §B, run 35951346282, 12 symbols × 180 days).

## What the code is
- NEXUS is a **deterministic indicator/rule ensemble**, not a trained model.
- `heuristic_win_probability` is an **uncalibrated** score transform. It is labelled as such in `nexus_probability_semantics` and in operator text, and its EV may only veto.
- The runtime NEXUS threshold is 60. The replay previously used 55 by mistake (fixed in P1-11).

## SIGNAL_EDGE
- Baseline strategy candidates: −0.375 R per candidate.
- NEXUS-approved: −0.324 R per candidate, authority CI [−0.496, −0.175].
- Every symbol, month and production regime is negative.
- Gross expectancy is already negative (−0.171 R); modeled costs add about −0.15 R.
- Break-even needs costs about 86% below the current model.

**NEGATIVE EXPECTANCY.**

## Threshold research (purged, embargoed; FINAL TEST untouched)
- 55 through 85 are all negative on TRAIN and on VALIDATION.
- TRAIN picked 55; VALIDATION rejected it, so the experiment stops there. FINAL TEST was not used to pick another threshold.
- **No threshold change is supported.**

## Gate ablation (dependence-aware verdicts)

| Component | Verdict | Δ expectancy R |
|---|---|---|
| MULTI_TIMEFRAME score | adds | +0.032 |
| TREND_ALIGNMENT score | adds | +0.018 |
| VOLATILITY score | adds | +0.012 |
| Regime-compatibility gate | adds | +0.005 |
| MOMENTUM score | **hurts** | −0.017 |
| RISK_REWARD score, VOLUME, MARKET_STRUCTURE, net-R:R gate, EV veto, missing-data-zero variant | no robust difference | — |
| DERIVATIVES / MICROSTRUCTURE | no effect (no historical data) | 0 |

- Two variants (net-R:R gate, missing-data-zero) look significant under IID. Under block bootstrap they are not.
- **Simplification candidates** are MOMENTUM (hurts), and RISK_REWARD, VOLUME, MARKET_STRUCTURE and the EV veto (no robust value).
- **Nothing was removed.** Removal needs its own evidence PR, and every effect here is tiny next to a −0.32 R strategy.
- Strategy-level gates (RSI, MACD, ADX, volume, EMA alignment, structure/BOS/CHoCH, MTF, adaptive MTF, pullback confirmation, strategy regime and R:R, pre-trade score) are **NOT ABLATED**. They are inline predicates in `Analyzer.analyze_mtf` and its runtime wrappers. A zero-semantic-change refactor into injectable predicates has to come first.

## Diagnostic slices (not permission to trade)
- Entry type PULLBACK: +0.132 R (447). MOMENTUM: −0.084. BOS_BREAK: −0.428 (3,121; 77% of approvals).
- SHORT: −0.509 R, against LONG −0.199.
- Higher 15m volume multiple goes with worse outcomes.
- These were found after the fact. Any hypothesis built on them (for example "PULLBACK-only") must be pre-registered and tested on data not used here.

## Exits (production facts, traced in the replay-parity phase)
- **TP1 == TP2 is production reality.** `calc_sl_tp` has no caller, and `Signal` sets `tp1 = tp2 = tp`. The production partial ignores `tp1`: it closes 50% at 1R + 0.03% and moves the stop to the fill price.
- **Native SL/TP sit at the unshifted signal levels.** The legacy replay shifted them by the fill delta; that was a replay bug.
- **The live exit stack** is native SL, native TP, 1R partial with break-even, trailing and the 2R exit.
- **Disabled or inactive in LIVE:** stagnation, CHoCH and regime exits are disabled by `operator_loss_policy`. The 90-minute hold is telemetry only. There is no time exit.
- **Trailing quirk.** After the partial, the excursion is read at twice its size because `peak_pnl` is not rescaled. The stop that results is often rejected for being on the wrong side of the mark (finding P1-17). This is not changed here; it belongs in the strategy phase.
- **Replay.** `nexus_oos_execution_parity.simulate_production_exit` reproduces these rules at 15m resolution. The EXIT_PARITY_MATRIX marks the bar-resolution rules APPROXIMATED.
- **Superseded:** the one-at-a-time exit variants from the historical model (4 h stagnation, 20/80-bar time exits). The parity model's variants are no_partial_tp, no_trailing, no_rr_double and shifted_native_stops_legacy_bug.

## Probability / EV
- Heuristic p: Brier 0.263 vs 0.222 for the base rate; ECE 0.20.
- The Platt slope is negative: higher confidence goes with lower realized win rate.
- Not promotable. The heuristic stays telemetry/veto only, and the EV veto was not removed.

## Replay parity before any strategy change
The same unchanged strategy is re-run through the parity replay (OOS_REPORT §C). Strategy research starts only after that result is recorded. If candidate expectancy stays materially negative, the verdict is STRATEGY_EDGE_FAILED.

## Research-mode next steps (candidate branch only)
1. (done in the replay) Production-parity exit model: partial TP, trailing, 2R, native stops. Remaining approximations are listed in the exit matrix.
2. Refactor strategy gates into injectable predicates (zero behavior change), then ablate them.
3. Pre-register a PULLBACK-only / LONG-only hypothesis and test it on fresh data, for example the next 90 days forward.
4. Collect historical OI and order-book data (or record them forward) to close the context-parity gap.
5. Shard the replay to reach 365+ days.
