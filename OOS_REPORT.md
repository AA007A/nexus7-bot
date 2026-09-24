# OOS_REPORT — factual out-of-sample evidence

This report only states numbers read from exact-SHA CI runs. Every section names its SHA and run ID. A later SHA's evidence never substitutes for an earlier one's, and the reverse is also true.

Layers are kept separate:

- **CANDIDATE_RESEARCH** (signal edge). Every strategy candidate is evaluated at a closed-candle decision time, and its net R is computed after modeled fees, slippage and funding. Candidates overlap in time, so they are not independent trades.
- **PORTFOLIO_EXECUTION_REPLAY** (executable edge). NEXUS-approved candidates are walked chronologically under production position and risk constraints. This layer exists only from `2932572` onward.

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

Run **35951346282**. PENDING: filled from the run once it completes.

---

## Verdict (as of b777cbb)

**NEGATIVE EXPECTANCY.**
- The NEXUS-approved candidates lose −0.239 R per trade net of modeled costs.
- Losses persist under every cost scenario, and break-even requires costs about 63% lower.
- NEXUS filters the baseline to a less-bad set. Uplift over a losing baseline is not edge.
- `AI_EDGE_NOT_PROVEN`.
