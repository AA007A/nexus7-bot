# OOS_REPORT — factual out-of-sample evidence

## Source

- Workflow: `NEXUS Real OOS Replay`, run [35467406970](https://github.com/AA007A/nexus7-bot/actions/runs/35467406970), job 105962172282, 2026-09-19. Commit `fcdac51` (PR #353, "Refresh NEXUS OOS evidence on current runtime").
- **It was not re-run for this branch.** The sandbox where this work was done cannot reach exchange hosts (proxy returns HTTP 403 for `api-futures.kucoin.com` and `fapi.binance.com`). Opening a PR from this branch will trigger the workflow, because this branch touches decision-affecting paths.
- The replay measures the **signal/NEXUS decision layer** in R multiples. It does not exercise the new position sizer, because R is independent of quantity. The sizing changes in this branch therefore do not change these numbers. They change how many dollars one R costs: at most `MAX_RISK_PCT` of equity, down from up to ~25%.

## Methodology, as reported by the artifact

`closed_candles_only=true`, `historical_clock_frozen=true`, `fees_included=true`, `slippage_included=true`, `funding_included_when_public_history_available=true`, `same_bar_ambiguity=STOP_FIRST`, `exchange_mutations=false`, `runtime_policy_mutations=false`.

Historical-context parity is **incomplete** for every symbol: `historical_open_interest=false`, `historical_orderbook=false`, `ticker_proxy=true`. Decisions that depend on OI or order-book context were therefore replayed with proxies.

## Universe

5 symbols × 2,500 15m candles (about 26 days each). 78 funding events per symbol.

| Symbol | Candidates | NEXUS approved | Rejected |
|---|---|---|---|
| BTCUSDT | 155 | 3 | 152 |
| ETHUSDT | 132 | 20 | 112 |
| SOLUSDT | 180 | 46 | 134 |
| XRPUSDT | 130 | 24 | 106 |
| DOGEUSDT | 206 | 33 | 173 |
| **Total** | **803** | **126** | **677** |

Counterfactual coverage: **1.0** (every rejected candidate has a known outcome).

## Pooled result (net of modeled costs)

| Metric | Value |
|---|---|
| Baseline expectancy (all eligible candidates) | **−0.703 R** |
| NEXUS-approved expectancy | **−0.526 R** |
| Expectancy uplift (NEXUS vs baseline) | +0.177 R |
| Paired bootstrap 95% CI of uplift | [−0.043, +0.410] R (robustness run: [−0.043, +0.418]) |
| CI strictly positive | **false** |
| Status | **`AI_EDGE_NOT_PROVEN`** |
| Blockers | `HISTORICAL_CONTEXT_PARITY_INCOMPLETE`, `UPLIFT_NOT_STATISTICALLY_POSITIVE` |

## Robustness (`ROBUSTNESS_RESEARCH_ONLY`, `promotion_authority=false`)

| Slice | Evaluated | Positive uplift | CI strictly positive |
|---|---|---|---|
| Per symbol | 5 | 2 | 0 |
| Leave-one-symbol-out | 5 | 5 | 0 |
| Temporal folds | 4 | 2 | 1 |

`stable_positive_point_estimate=false`.

## Interpretation

1. **The approved set has negative net expectancy (−0.53 R per trade).** On this evidence, trading the current strategy loses money after costs. A lower loss per trade from the new sizing reduces how fast it loses. It does not make the strategy profitable.
2. NEXUS appears to filter out some bad candidates (point uplift +0.18 R), but the CI includes zero, and only 1 of 4 temporal folds and 0 of 5 symbols are individually significant. **INSUFFICIENT EVIDENCE** of incremental edge.
3. BTC produced 3 approvals out of 155 candidates. No per-symbol conclusion is possible for BTC.
4. ~26 days per symbol is a single market regime window. Regime segmentation (bull/bear/range/high-vol/low-vol/breakout/choppy) is **not reported** by the current artifact and is **INSUFFICIENT EVIDENCE**.

## Items requested but not available

| Requested | Status |
|---|---|
| Win rate, avg winner/loser, payoff ratio, profit factor, max DD, Sharpe/Sortino, tail loss per threshold | Not in the latest artifact's printed summary. The PR #321 workflow added profit factor / win rate / cost stress to the research module, but those fields were not printed in run 35467406970. **Not reported here, to avoid inventing numbers.** |
| Threshold sweep (60/65/75/85) | Not run. **No threshold change is justified**, and none was made. |
| Probability calibration (Brier, log loss, reliability curve) | Not run. `nexus_probability` is a documented heuristic (`ensemble_linear_v1`, `p = min(0.75, 0.30 + 0.45·c/100)`). It is **not** a calibrated probability. With 126 approved outcomes, isotonic calibration would overfit. Platt scaling on a train split is possible only after a longer history is collected. |
| Regime segmentation | Not available. |
| 12-symbol universe | The last run used 5 symbols. The 12-symbol batch (PR #315) is older than the current runtime. |

## Verdict

`AI_EDGE_NOT_PROVEN`. Net expectancy is negative for both baseline and NEXUS-approved candidates. No production threshold or "AI improvement" claim is supported by this evidence.
