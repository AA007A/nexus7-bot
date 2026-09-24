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

## Exits
- One-at-a-time variants: a 4 h stagnation exit and a 20-bar time exit are both worse. An 80-bar time exit is about equal to current.
- **Parity gap P1-13:** signals carry tp1 == tp2, so partial TP and break-even are never exercised by the replay.
- Trailing stop, CHoCH exit, regime/signal invalidation and the 90-minute min-hold are **not modeled**.
- Hard SL is modeled first on every bar (same-bar ambiguity resolves STOP_FIRST).

## Probability / EV
- Heuristic p: Brier 0.263 vs 0.222 for the base rate; ECE 0.20.
- The Platt slope is negative: higher confidence goes with lower realized win rate.
- Not promotable. The heuristic stays telemetry/veto only, and the EV veto was not removed.

## Research-mode next steps (candidate branch only)
1. Build a production-parity exit model: partial TP, trailing, CHoCH, stagnation and min-hold.
2. Refactor strategy gates into injectable predicates (zero behavior change), then ablate them.
3. Pre-register a PULLBACK-only / LONG-only hypothesis and test it on fresh data, for example the next 90 days forward.
4. Collect historical OI and order-book data (or record them forward) to close the context-parity gap.
5. Shard the replay to reach 365+ days.
