# OOS_REPORT — factual out-of-sample evidence

This report only states numbers read from exact-SHA CI runs. Every section names its SHA and run ID. A later SHA's evidence never substitutes for an earlier one's, and the reverse is also true.

Layers are kept separate:

- **CANDIDATE_RESEARCH** (signal edge). Every strategy candidate is evaluated at a closed-candle decision time, and its net R is computed after modeled fees, slippage and funding. Candidates overlap in time, so they are not independent trades.
- **PORTFOLIO_EXECUTION_REPLAY** (executable edge). NEXUS-approved candidates are walked chronologically under production position and risk constraints. This layer exists only from `2932572` onward. From `a761d80` it is a bar-by-bar event engine with production execution parity (section C).

---

## A. Exact-SHA evidence — `b777cbb97948cae6ec7e94b516a56a432719dca1`

Workflow `NEXUS Real OOS Replay` run **35946377972**, job 107465124811, 2026-09-24.
- The replay step took 67 min and succeeded. The evidence summary and upload also succeeded.
- **The strict promotion gate failed with exit 1.** That failure is the gate working as designed, not an engine bug.
- This SHA predates dependence-aware inference, purged splits and the portfolio layer. Its intervals are **IID row-bootstrap** intervals: diagnostic only, with no promotion authority.

### Universe and data integrity
- 12/12 production symbols with **no unavailable symbols**: BTC, ETH, SOL, XRP, ADA, DOGE, LINK, AVAX, DOT, LTC, NEAR, ATOM.
- 8,640 15m candles each (about 90 days, 2026-06-26 → 2026-09-24).
- Replay threshold was **60.0**, the production `nexus_ai.MIN_SCORE`. Before `d548084` the replay used 55.
- Historical context parity is **incomplete** for every symbol: no historical OI, no order book, ticker proxy only.

### Candidate layer

| | Baseline (all candidates) | NEXUS-approved | Rejected |
|---|---|---|---|
| Candidates | 8,920 | 1,946 (approval rate 21.8%) | 6,974 |
| Net expectancy | **−0.360 R** | **−0.239 R** | −0.394 R |
| IID 95% CI of expectancy | [−0.389, −0.332] | [−0.316, −0.167] | [−0.427, −0.361] |
| Gross expectancy (before fees/funding) | −0.173 R | −0.090 R | −0.197 R |
| Win / loss / breakeven rate | 30.7% / 69.2% / 0.2% | 29.1% / 70.7% / 0.2% | 31.1% / 68.8% / 0.1% |
| Avg winner / avg loser | 1.756 / −1.298 R | 2.235 / −1.259 R | 1.630 / −1.310 R |
| Median R | −1.213 | −1.178 | −1.221 |
| Payoff ratio / profit factor | 1.35 / 0.60 | 1.77 / 0.73 | 1.24 / 0.56 |
| p05 / p01 / CVaR 5% | −1.59 / −1.82 / −1.75 R | −1.48 / −1.52 / −1.50 R | −1.63 / −1.90 / −1.79 R |
| Fees / slippage / funding (sum, R) | −1664 / −2311 / −5.1 | −290 / −416 / −1.3 | −1374 / −1895 / −3.8 |
| Longest losing / winning streak | 87 / 36 | 39 / 20 | 73 / 29 |
| Per-trade Sharpe / Sortino (not annualized) | −0.25 / −0.33 | −0.15 / −0.22 | −0.28 / −0.36 |

NEXUS uplift vs baseline: **+0.121 R**, IID paired bootstrap [+0.058, +0.181]. That IID interval overstates confidence; see `93631df` for the dependence-aware version.

### Cost stress (approved set, net expectancy R)

| current | fees +25% | fees +50% | slippage ×1.5 | slippage ×2 | combined (fees +50%, slip ×2) |
|---|---|---|---|---|---|
| −0.239 | −0.277 | −0.314 | −0.355 | −0.464 | −0.538 |

**Break-even cost multiplier:** 0.368× current costs for the approved set, 0.198× for baseline. Fees and slippage are scaled together. The strategy needs costs about 63% below today's model before it breaks even. There is no cost-stress survival.

### Exit variants (approved, net R; one change at a time)
- current −0.239
- no partial TP −0.239
- partial without break-even −0.239
- 4h stagnation exit −0.307
- 20-bar time exit −0.281
- 80-bar time exit −0.237

The partial-TP and break-even variants are identical to current because analyzer signals carry `tp1 == tp2`, so the replay never exercises partial exits. Parity gap P1-13.

### Robustness (IID era)
- Temporal folds with positive uplift: 2 of 4; with a strictly positive CI: 1 of 4.
- Symbols with positive uplift: 8 of 12; with a strictly positive CI: 3 of 12.
- Leave-one-symbol-out: 12 of 12 positive.
- Concentration of the approved set:
  - Only 3 of 12 symbols have positive total R; AVAXUSDT supplies 63% of positive R.
  - 0 of 4 months are positive.
  - Research regime HIGH_VOLATILITY supplies 64% of positive R.

### Segments (approved, net R, n)
- **Direction:** LONG −0.077 (1,517); SHORT **−0.812** (429).
- **Entry type:** PULLBACK +0.170 (228, IID lower bound −0.003); MOMENTUM −0.009 (238); BOS_BREAK **−0.340** (1,480).
- **Research regime:**
  - Positive: HIGH_VOLATILITY +0.467 (150, IID lower +0.183); TRENDING_BULL +0.061 (627); EXTREME_EVENT +0.273 (7).
  - Negative: BREAKOUT −0.204 (610); RANGE −0.723 (179); TRENDING_BEAR −0.918 (227); BREAKDOWN −0.898 (106); CHOPPY −0.700 (25); LOW_VOLATILITY −0.037 (15).
- **Symbol:** AVAX +0.068, XRP +0.025, SOL +0.022; every other symbol is negative, BTC at −0.315 (36).
- **15m ATR% bucket:** ≥0.8% +0.020 (769); <0.4% −0.574 (343).

These are diagnostic slices. They are **not** permission to trade a profitable subset; a subset chosen after the fact is in-sample.

### Threshold research (old unpurged split; superseded by `93631df`)
No threshold is positive on both TRAIN and VALIDATION:

| Threshold | Train n / exp | Validation n / exp | Test n / exp |
|---|---|---|---|
| 55 | 632 / −0.534 | 741 / −0.060 | 720 / −0.136 |
| 60 | 594 / −0.507 | 694 / −0.093 | 658 / −0.153 |
| 70 | 455 / −0.447 | 505 / −0.157 | 466 / −0.151 |
| 80 | 266 / −0.304 | 248 / −0.309 | 219 / −0.062 |
| 85 | 86 / −0.335 | 93 / −0.622 | 91 / +0.210 |

Old selection rule picked 80, with no stable plateau. **INSUFFICIENT EVIDENCE for any threshold change.** The runtime stays at 60.

### NEXUS component ablation (IID era; Δ = full − ablated, positive means the component adds expectancy)
- IID-positive:
  - no_rr_net_gate +0.067
  - minus_MULTI_TIMEFRAME +0.036
  - minus_TREND_ALIGNMENT +0.033
  - minus_RISK_REWARD +0.024
  - minus_VOLATILITY +0.020
- IID-negative: minus_MOMENTUM −0.020.
- Other components are null:
  - The EV gate and regime-compat gate change fewer than 20 approvals, with Δ ≈ 0.
  - MICROSTRUCTURE and DERIVATIVES contribute nothing (0 Δ).
- Context ablation:
  - Candle-only vs full available context: Δ +0.004, 15 approvals.
  - Adding funding: 0 Δ.

These verdicts are **recomputed with block-bootstrap authority** on `2932572`; the IID verdicts above have no authority.

### Probability (heuristic) on the test split
- Heuristic p: Brier **0.261**, log-loss 0.716, **ECE 0.205**. Mean predicted is about 0.5 against an observed win rate of about 0.29.
- Base-rate predictor: Brier 0.214, log-loss 0.619.
- Platt refit: Brier 0.2135, only marginally below base rate, with **negative slope (−0.188)**. Higher NEXUS confidence did not predict a higher win rate.
- Conclusion: the heuristic is miscalibrated and overconfident. It stays telemetry/veto-only, and calibration is not promotable.

### Strict gate blockers (b777cbb)
- `APPROVED_EXPECTANCY_NOT_POSITIVE`
- `APPROVED_EXPECTANCY_CI_NOT_POSITIVE`
- `COST_STRESS_FAILS_FEES_PLUS_50PCT`
- `COST_STRESS_FAILS_SLIPPAGE_X2`
- `HISTORICAL_CONTEXT_PARITY_INCOMPLETE`
- `SINGLE_PERIOD_DOMINATES`
- `SINGLE_SYMBOL_DOMINATES`
- `STATUS_NOT_AI_EDGE_PROVEN`
- `TEMPORAL_ROBUSTNESS_INSUFFICIENT`
- `TOO_FEW_SYMBOLS_CONTRIBUTING`

---

## B. Exact-SHA evidence — `29325728481f2a9aa5e64d5228174c44a0e30c9b` (180 days)

Workflow `NEXUS Real OOS Replay` run **35951346282**, job 107480363889, 2026-09-24.
- The replay step took 87 min and succeeded. The evidence summary and upload also succeeded.
- **The strict promotion gate failed with exit 1.** That failure is the gate working as designed; the run itself had no engineering error.
- From this SHA onward, **promotion authority rests on dependence-aware intervals**. IID intervals are shown for comparison only.

### Universe and data integrity
- 12/12 symbols with **no unavailable symbols**.
- 17,280 15m candles each: **180 days, 2026-03-28 → 2026-09-24**, with pagination integrity checks passing.
- Seven calendar months, covering production regimes TRENDING_BULL, TRENDING_BEAR, RANGE, BREAKOUT, BREAKDOWN, ACCUMULATION and HIGH_VOLATILITY.
- Threshold 60.0. Context parity incomplete, since there is no historical OI or order book.
- 365 days was not attempted: 180 days takes 87 min, so a full year would exceed a single job and needs sharding. **INSUFFICIENT EVIDENCE of multi-year durability.**

### Effective sample size

| | Raw rows | Unique UTC days / 24 h blocks | Symbols | Intra-block correlation | Design effect | Effective n |
|---|---|---|---|---|---|---|
| Baseline candidates | 17,617 | 179 | 12 | 0.129 | 13.5 | **1,301** |
| NEXUS-approved | 4,077 | 171 | 12 | 0.095 | 3.18 | **1,283** |

- Lag-1 autocorrelation of daily block mean R is **0.21**, above the 0.153 significance band. Dependence reaches past 24 h, which is why the 48 h block interval is also computed and the authority interval takes the more conservative of the two.
- The maximum observed outcome horizon is 10.5 h.

### CANDIDATE_RESEARCH — signal edge

| | Baseline | NEXUS-approved |
|---|---|---|
| Candidates | 17,617 | 4,077 (23.1%) |
| Net expectancy | **−0.375 R** | **−0.324 R** |
| IID CI (diagnostic) | [−0.395, −0.352] | [−0.373, −0.271] |
| 24 h block CI | [−0.489, −0.269] | [−0.463, −0.199] |
| 48 h block CI | [−0.515, −0.241] | [−0.496, −0.175] |
| **Authority CI** | **[−0.515, −0.241]** | **[−0.496, −0.175]** |
| Gross expectancy | −0.193 R | −0.171 R |
| Win / loss / breakeven | 30.0% / 69.9% / 0.1% | 26.6% / 73.3% / 0.1% |
| Avg winner / loser | 1.764 / −1.294 R | 2.254 / −1.259 R |
| Median R | −1.215 | −1.187 |
| Payoff / profit factor | 1.36 / 0.585 | 1.79 / 0.649 |
| p05 / p01 / CVaR 5% | −1.56 / −1.78 / −1.70 | −1.48 / −1.52 / −1.51 |
| Fees / slippage / funding (sum R) | −3208 / −4362 / −4.5 | −621 / −902 / −1.6 |
| Longest losing / winning streak | 89 / 36 | 72 / 20 |
| `candidate_sequence_drawdown_r` (overlapping candidates, **not** account DD) | 6,740 | 1,338 |
| Per-trade Sharpe / Sortino (not annualized) | −0.26 / −0.34 | −0.20 / −0.30 |

**NEXUS uplift over baseline: +0.051 R.**

| Interval | Uplift CI |
|---|---|
| IID (diagnostic) | [**+0.009**, +0.096] |
| 24 h block | [−0.040, +0.135] |
| 48 h block | [−0.043, +0.143] |
| **Authority** | **[−0.043, +0.143], not significant** |

An IID reading would have wrongly called this uplift statistically significant. Once cross-symbol, same-day dependence is respected, it is not.

### Concentration and robustness (approved set)
- **0 of 12 symbols**, **0 of 7 months** and **0 of 7 production regimes** have positive total R.
- Temporal folds with positive uplift: 3 of 4; with a strictly positive IID CI: 1 of 4.
- Leave-one-symbol-out: 12 of 12 positive uplift.
- Every one of these is uplift over a losing baseline, not profit.

### Cost stress (net expectancy R)

| Set | current | fees +25% | fees +50% | slippage ×1.5 | slippage ×2 | combined |
|---|---|---|---|---|---|---|
| Approved | −0.324 | −0.362 | −0.400 | −0.443 | −0.568 | −0.644 |
| Baseline | −0.375 | −0.421 | −0.466 | −0.504 | −0.635 | −0.726 |

**Break-even cost multiplier:** 0.138× current costs for approved and 0.130× for baseline. The approved set would break even only if modeled fees and slippage were about 86% lower.

### Exit variants (approved, one change at a time)
- current −0.324
- no partial TP −0.324
- partial without break-even −0.324
- 4 h stagnation exit −0.357
- 20-bar time exit −0.347
- 80-bar time exit −0.319

Partial-TP and break-even variants are no-ops because signals carry tp1 == tp2 (parity gap P1-13). Trailing, CHoCH exit, regime/signal invalidation and min-hold are not modeled. No exit change is supported.

### Segments (approved; production regime is primary)
- **Production regime:** every regime is negative.
  - BREAKOUT −0.001 (495), HIGH_VOLATILITY −0.124 (108), TRENDING_BULL −0.243 (1,483).
  - BREAKDOWN −0.438 (225), TRENDING_BEAR −0.467 (967), ACCUMULATION −0.482 (149), RANGE −0.499 (650).
- **Research regime (diagnostic):** LOW_VOLATILITY +0.912 (32); every other research regime is negative.
- **Direction:** LONG −0.199 (2,436); SHORT −0.509 (1,641).
- **Entry type:** PULLBACK +0.132 (447); MOMENTUM −0.084 (509); BOS_BREAK −0.428 (3,121).
- **Symbol:** all 12 negative, from −0.183 (AVAX) to −0.607 (LTC).
- **NEXUS confidence:** no monotone improvement; ≥80 gives −0.632 (89).
- **15m volume multiple:** expectancy falls as volume rises (<0.5× +0.030, ≥2.5× −0.430).

These are diagnostics. The positive slices (PULLBACK, the LOW_VOLATILITY research regime) were found after the fact and are untested out of sample. They are **not** a basis for trading.

### Threshold research (purged and embargoed)
- Split: TRAIN 8,648 rows, then a 10.5 h embargo, VALIDATION 3,439, then another 10.5 h embargo, FINAL TEST 5,460.
- Purged rows: 1 from TRAIN and 3 from VALIDATION.
- Every threshold is negative on both TRAIN and VALIDATION (authority lower bounds between −0.58 and −0.86).
- TRAIN selected 55 (not a stable plateau). VALIDATION did not confirm it (−0.604 R), so **FINAL TEST was never consulted**.
- Status: `NOT_CONFIRMED_ON_VALIDATION`. The runtime threshold stays at 60 (unchanged).

### NEXUS ablation (Δ = full − ablated; verdict uses the authority interval)

| Variant | Δ approvals | Δ exp R | IID CI | 24 h block CI | Verdict |
|---|---|---|---|---|---|
| minus MULTI_TIMEFRAME | +336 | +0.032 | [+0.017, +0.048] | [+0.013, +0.050] | ADDS |
| minus TREND_ALIGNMENT | −103 | +0.018 | [+0.004, +0.031] | [+0.001, +0.032] | ADDS |
| minus VOLATILITY | +161 | +0.012 | [+0.001, +0.023] | [+0.000, +0.024] | ADDS |
| no regime-compat gate | −34 | +0.005 | [+0.002, +0.009] | [+0.002, +0.009] | ADDS |
| minus MOMENTUM | +24 | −0.017 | [−0.029, −0.005] | [−0.028, −0.003] | HURTS |
| minus RISK_REWARD | +261 | +0.011 | [−0.001, +0.025] | [−0.002, +0.025] | no robust difference |
| no net-R:R gate | −4,428 | +0.040 | [**+0.008**, +0.075] | [−0.017, +0.097] | no robust difference (IID alone would have said ADDS) |
| missing optional → 0 credit | +1,876 | +0.047 | [**+0.003**, +0.092] | [−0.016, +0.105] | no robust difference (IID alone would have said ADDS) |
| no EV veto | −31 | +0.001 | [−0.003, +0.005] | [−0.003, +0.005] | no robust difference |
| minus VOLUME / MARKET_STRUCTURE | −156 / +20 | −0.011 / −0.009 | CI ∋ 0 | CI ∋ 0 | no robust difference |
| minus DERIVATIVES / MICROSTRUCTURE | 0 | 0 | — | — | no effect (no historical data) |
| context A candle-only | +45 | +0.004 | [−0.003, +0.011] | [−0.003, +0.010] | no robust difference |
| context B +funding / D full available | 0 | 0 | — | — | no effect |

**Interpretation.**
- Some NEXUS components improve on the baseline by 0.005–0.03 R. MOMENTUM makes it worse.
- These are small effects on a strategy that loses about 0.32 R per trade.
- The EV veto has no measurable value.
- The unavailable context has no measurable effect on these replayable inputs.

No production gate was removed.

### Probability calibration (purged TRAIN 1,644 / CALIBRATION 878 / FINAL TEST 1,553)

| Predictor on FINAL TEST | Brier | Log-loss | ECE |
|---|---|---|---|
| Heuristic p | 0.263 | 0.719 | 0.201 |
| Base rate (0.237) | **0.222** | **0.639** | — |
| Platt fitted on CALIBRATION | 0.223 | 0.643 | 0.089 |

- The Platt slope is **negative (−0.53)**: higher NEXUS confidence goes with *lower* realized win rates.
- The 24 h block CI of the Brier improvement is [−0.0027, +0.0001].
- Status: `CALIBRATION_NOT_PROMOTABLE_HEURISTIC_REMAINS_TELEMETRY`.

### PORTFOLIO_EXECUTION_REPLAY — executable edge
Policy:
- Production-reported leverage 50x and MAX_DRAWDOWN 50%.
- Canonical MAX_RISK_PCT 1%, MAX_MARGIN_PCT 10%, operator margin cap 50%, MAX_POSITIONS 2, daily stop 3%.
- Starting equity 1,000.
- Contract metadata from current public KuCoin data for all 12 symbols.

| Metric | Value |
|---|---|
| Starting → ending equity | 1,000.00 → **505.69** |
| Net return | **−49.4%** |
| Portfolio max drawdown | **50.3%**; the drawdown hard gate then blocked all later entries (duration 4,288 h) |
| Trades executed | 123 of 4,077 approved candidates |
| Skipped: drawdown gate / position limit / same symbol open / daily stop / capital-risk | 3,736 / 121 / 26 / 71 / 0 |
| Net expectancy | −0.788 R per trade; authority CI [−1.076, −0.468] |
| Win rate / avg winner / avg loser | 15.5% / 2.51 R / −1.39 R |
| Profit factor | 0.34 |
| Fees / slippage / funding (quote) | −145.0 / −121.6 / 0.0 |
| Longest losing streak | 18 |
| Exposure time / avg capital utilization | 23.4% / 0.75% |
| Max concurrent positions | 2 (= MAX_POSITIONS) |
| By month | 2026-03: 11 trades, −74.3; 2026-04: 112 trades, −420.0; no trades after the drawdown gate |
| By symbol / direction / production regime | every group negative |

The executable strategy would have reached the 50% drawdown hard gate within about 5 weeks of the replay start, and would then have been blocked for the rest of the window.

### Strict gate blockers (`2932572`)
- `APPROVED_EXPECTANCY_BLOCK_CI_NOT_POSITIVE`
- `APPROVED_EXPECTANCY_NOT_POSITIVE`
- `COST_STRESS_FAILS_FEES_PLUS_50PCT`
- `COST_STRESS_FAILS_SLIPPAGE_X2`
- `HISTORICAL_CONTEXT_PARITY_INCOMPLETE`
- `PORTFOLIO_DRAWDOWN_EXCEEDS_RESEARCH_LIMIT`
- `PORTFOLIO_EXPECTANCY_NOT_POSITIVE`
- `PORTFOLIO_FINAL_EQUITY_NOT_ABOVE_START`
- `PORTFOLIO_ROBUSTNESS_CI_NOT_POSITIVE`
- `SINGLE_PERIOD_DOMINATES`
- `SINGLE_SYMBOL_DOMINATES`
- `STATUS_NOT_AI_EDGE_PROVEN`
- `TOO_FEW_PORTFOLIO_SYMBOLS_CONTRIBUTING`
- `TOO_FEW_SYMBOLS_CONTRIBUTING`
- `UPLIFT_BLOCK_CI_NOT_POSITIVE`

---

## C. Replay-parity phase (code `a761d80` and later)

This section describes **methodology and code**. It deliberately contains no run IDs for the new code: committing them would change the SHA they prove. The exact run IDs and the old-vs-new numbers for each immutable head are posted as a PR comment and printed in the Actions summary ("Show evidence summary").

Historical final-head evidence under the previous code: `70b00b4`, run 35957755830 (replay OK, strict gate BLOCK). NEXUS-approved −0.3236 R, block authority CI [−0.495, −0.179]. Uplift +0.051 R, CI [−0.049, +0.132]. Portfolio 1000 → 505.69 (−49.4%), max drawdown 50.3%, 123 trades, −0.788 R per trade.

### C.1 Statistical authority (P0-STAT-01/02)
- **Authority:** min lower / max upper over VALID UTC-block intervals of predeclared lengths 24h, 48h and 72h.
- **IID:** reported as `DIAGNOSTIC_ONLY` and never enters the authority.
- **Gate:** recomputes the authority from the block intervals and blocks on any mismatch. It never reads the legacy IID report and ignores legacy IID blocker codes. It requires `authority_model = BLOCK_BOOTSTRAP_ONLY_V1`.
- **Legacy edge report:** moved to `legacy_diagnostic` with `authority: NONE`.
- **Status separation:** `candidate_research.research_status` is the research result; the gate verdict is the only promotion authority.
- **Dependence diagnostics:** daily-block autocorrelation at lags 1–3 and lag-1 autocorrelation per block length. These support the block lengths but do not select them.

### C.2 Production facts established from the composed LIVE runtime
| Fact | Source | Consequence for the replay |
|---|---|---|
| TP1 == TP2 in every production signal | `calc_sl_tp` has no caller; `Signal.__post_init__` sets `tp1 = tp2 = tp` | Not a replay bug; the partial is driven by the 1R rule instead |
| Partial TP at `fill ± (|fill − sl| + fill·0.0003)`, 50%, then stop → fill | `engine._manage_partial_tp`, `partial_tp_execution_hardening` | Replayed |
| Native SL/TP at the **unshifted** signal levels (entry order triggers); local levels are shifted by the fill delta | `kucoin_native_tpsl`; `engine._open` shifts `sig.sl/tp` after `place_order` | Legacy replay shifted them (bug); corrected |
| No time exit | exit stack | Legacy 40-bar exit removed (research censoring cap only) |
| Stagnation, CHoCH and regime exits disabled in LIVE | `operator_loss_policy.no_discretionary_loss_exit` installed last | Not applied |
| 90 min minimum hold is telemetry only | `_sync_positions` logs only | Not applied |
| 2R exit uses the CURRENT stop distance (inert after break-even) | `confirmed_rr_exit.check` | Replayed |
| Trailing: gives back `TRAILING_LOCK` of the peak excursion; `peak_pnl` is not rescaled after the partial; a stop on the wrong side of the mark is rejected | `trailing_safety_hardening`, `native_stop_repair` | Replayed as-is (finding P1-17) |
| Post-NEXUS liquidation-safe geometry: compress SL/TP proportionally when ≥ 40% of the stop remains, re-run NEXUS, else block | `kucoin_contract_risk_hardening` | Replayed with public MMR as proxy |
| At most ONE concurrent LIVE position | `engine._open` `liquidation.analyze(n_open_positions=len+1)`; override forbidden | Replayed; CROSS stress is unreachable |
| Pilot: 2 concurrent positions, 2 new orders per process session | `bot.pilot` | Concurrent cap replayed; session cap NOT_REPLAYABLE |
| Legacy pre-trade score is advisory after exact NEXUS approval | `legacy_pretrade_advisory` | Not authoritative |
| Daily PnL = realized today (UTC) + full unrealized of open positions; limit from current equity | `daily_stop_runtime_hardening` | Replayed (plus equity-anchored sensitivity) |
| Sizing at the SIGNAL entry/stop with drift allowance and the conservative cost model | `final_sizing_invariants.size_pilot_entry` | Replayed; parity proven on identical snapshots |
| Analyzer drops the last candle; replay receives only closed candles | `market_data_integrity` appends a disposable sentinel | Verified (`closed_candle_sentinel_verified`) |

The full classification is in the artifact: `replay_parity.exit_parity_matrix` and `replay_parity.pretrade_gate_matrix`. Classes: FULLY_REPLAYED / APPROXIMATED / NOT_REPLAYABLE / LIVE_ONLY / INACTIVE_IN_LIVE / TELEMETRY_ONLY / NOT_AUTHORITATIVE_IN_LIVE / UNREACHABLE.

**Parity status: INCOMPLETE.**
- **Exits:** native SL/TP, partial, break-even, trailing and 2R are APPROXIMATED at 15m resolution; restart state is LIVE_ONLY.
- **Pre-trade:** NEXUS context, CROSS MMR, drift and viability are APPROXIMATED. Spread/depth, market-risk feeds and the pilot session cap are NOT_REPLAYABLE; operational pilot checks are LIVE_ONLY.
- **Consequence:** the gate therefore blocks with `EXIT_PARITY_INCOMPLETE`, `PRETRADE_CONTEXT_PARITY_INCOMPLETE` and `PORTFOLIO_PARITY_INCOMPLETE`.

### C.3 Candidate research on production-parity outcomes
- **Baseline:** every production-EXECUTABLE strategy candidate, meaning geometry SAFE or ADJUSTED, regime allows the direction, expected PnL > 0, session-adjusted score ≥ minimum, and drift within the limit. Each is evaluated as if executed.
- **Approved:** NEXUS approval on the original geometry, plus re-approval on the adjusted geometry when compressed.
- **Outcome:** production exit emulation with fees, slippage and funding at actual timepoints. R is measured against the planned stop risk.
- **Step-wise attribution on identical data (`parity_attribution`):**
  - **A0:** legacy model.
  - **A1:** + unshifted native stops.
  - **A2:** + production exit engine.
  - **A3:** + liquidation-safe geometry with NEXUS re-approval.
  - **A4:** + production funnel (= production approval).
- **Legacy portfolio engine:** re-run on the same data (`portfolio_replay_legacy`).

### C.4 Portfolio engine v2
- **Timeline:** every closed 15m bar from the first decision to the last exit.
- **Precedence at bar close T:** FUNDING → EXITS → MARK → DAILY_RESET (00:00 UTC) → ACCOUNT (equity, research HWM, drawdown, daily PnL, circuit breakers) → CANDIDATES (score × rr desc, symbol) → ENTRY at the next open.
- **Accounting identities asserted at every step:**
  - equity = cash + unrealized;
  - available = equity − committed margin;
  - cash ledger = start + realized − fees + funding;
  - margin released pro rata exactly once;
  - no mark or funding after close;
  - events on the 15m grid.
- **Robustness authority:** path bootstrap. UTC-block-resampled candidate timelines (24/48/72 h) are re-run through the full state machine; the most conservative lower bound of net return is used. The trade-level CI is kept with `authority: NONE`.
- **Sensitivities:**
  - daily-PnL semantics;
  - one production gate disabled at a time (attribution);
  - contract specs (lot ×10, multiplier ×10, 10 USDT minimum notional, MMR ×2). Specs are `CURRENT_CONTRACT_SPEC_PROXY`; no historical specs are available.

### C.5 Pinned policy
- **Source:** `research/replay_policy_manifest.json` holds non-secret values only, each with its provenance.
- **Artifact:** `replay_policy_manifest.policy_sha256`.
- **Fail-closed checks:** the CLI exits 2 when the manifest is missing or malformed, when a key is missing, extra or mistyped, when the risk policy is invalid, or when a value production reads in-process differs from the runtime.

### C.6 365-day architecture (design; not implemented in this phase)
A single 365-day job does not fit the 350-minute CI budget. The design keeps path dependence:
1. **History stage.** Fetch 15m/1h/4h candles, funding and contract specs per symbol for the whole horizon. Verify contiguity, then write a content-addressed cache (hash of symbol, interval, start, end).
2. **Candidate shards.** Split into (symbol × month) shards. Each shard replays decisions inside its window, using warm-up candles from the cache before the window and forward candles after it, so that exits crossing the boundary are complete. The shard emits candidate rows with their full production-parity paths. Shards are pure functions of the cache and the manifest hash.
3. **Deterministic merge.** Concatenate the rows and sort by (decision_ts, symbol). Reject duplicates or gaps. Record the merged hash.
4. **Global inference.** Block bootstrap, purged splits and ablations run on the merged rows. Never run them per shard and average.
5. **Central portfolio replay.** Run the event engine once over the merged, globally ordered event stream. Positions crossing a shard boundary keep their state because the engine never sees shard boundaries. Per-shard portfolio results are never averaged.

### C.7 Results
The new numbers (A0 through A4, new vs old portfolio, path bootstrap, parity blockers) are reported per exact SHA in the PR comment. The strategy was not changed. Any difference from the historical evidence above is attributed by `parity_attribution` and `portfolio_replay.gate_attribution`.

---

## Verdict

**NEGATIVE EXPECTANCY** in both the candidate layer and the portfolio layer, on 12 symbols over 180 days.

| Layer | Result |
|---|---|
| SIGNAL_EDGE | NEXUS-approved −0.324 R per candidate; authority CI [−0.496, −0.175] |
| STATISTICAL_CONFIDENCE | NEXUS uplift +0.051 R is **not significant** under dependence-aware inference (authority CI [−0.043, +0.143]) |
| PORTFOLIO_EDGE | −49.4% equity, 50.3% drawdown, then halted by the drawdown hard gate |
| CONTEXT_PARITY | Incomplete: no historical OI or order book. Ablation shows no measurable effect of the replayable optional context |

Status: `AI_EDGE_NOT_PROVEN`.

The verdict above is from the historical (pre-parity) model. Section C describes the parity rerun of the same unchanged strategy; its exact-SHA result is in the PR comment. `REPLAY_PARITY = INCOMPLETE` by construction (C.2).
