# STRATEGY_VALIDATION

This pass changed **no strategy threshold, gate, indicator or NEXUS weight**. A HOLD is a valid decision. Changing gates to raise trade frequency was out of scope and is not supported by evidence (OOS_REPORT.md).

## What the code is

- NEXUS is a **deterministic indicator/rule ensemble** (`nexus_ai.py`, `nexus_models.py`). It is not a trained ML model. That is acceptable, and it should be described that way.
- `nexus_probability.heuristic_win_probability` is a versioned **heuristic** (`ensemble_linear_v1`). Its docstring already states that it is "not an empirically calibrated probability". Any UI or notification that shows it as "win probability" should say "heuristic score-derived p". (Follow-up: the `notifier` wording was not audited in this pass.)
- Score grades in `nexus_ai.py`: 90+ A+, 85–89 A, 75–84 B, 65–74 C, <65 NO_TRADE. The runtime minimum is `NEXUS_MIN_SCORE` = `MIN_ENTRY_SCORE` = 60, which the source documents as favoring frequency over selectivity. **Not changed.** No OOS threshold sweep exists to justify any other value.

## Gate audit status

| Area | Status in this pass | Evidence / note |
|---|---|---|
| Closed-candle semantics / no forming-candle contamination | Existing protection, verified by the passing suite | `market_data_integrity`, `pretrade_hardening` (drops the final forming candle), `test_pretrade_fail_closed`, OOS `closed_candles_only=true` |
| Lookahead in replay | Existing | `historical_clock_frozen=true`, `test_nexus_oos_replay_full_clock` |
| Stale data fail-closed | Existing | `market_data_integrity`, `derivatives_news_freshness_hardening` |
| Optional-data renormalization bias | Partially existing | `nexus_optional_evidence`. Not re-audited. |
| Correlated indicators counted as independent votes (RSI/MACD/ADX/volume/structure) | **Not audited** | Requires an ablation replay per gate |
| Regime double counting (regime + 4H + adaptive MTF) | **Not audited** | same |
| External providers fabricating conviction | Verified for CoinGlass: failed provider ⇒ no signal (fail-neutral) | `test_market_risk_runtime` |
| Cost-adjusted EV / R:R gate | Existing. Costs included in replay | `rr_precision_hardening`, `nexus_live_cost_calibration` |

## Exit engine

| Item | Status |
|---|---|
| Hard SL | Native KuCoin TPSL installed with the entry and verified by readback (`kucoin_native_tpsl`) |
| Min-hold (90 min) vs CHoCH/regime exits | Existing. Min-hold applies to discretionary exits only; native SL is exchange-side and unaffected. **Not re-verified OOS.** |
| Stagnation 4h | Existing. **Not validated OOS.** |
| Partial TP / BE / trailing | Existing durable + idempotent paths (`durable_partial_exit`, `partial_tp_execution_hardening`) |
| Exit precedence table | Not produced in this pass |

## Required next steps (all evidence-first)

1. Re-run `oos_real_replay` on this branch (it triggers automatically on a PR).
2. Extend the artifact to print win rate, payoff, PF, max DD and tail loss per threshold, plus regime labels.
3. Run a per-gate ablation replay to find duplicated or correlated evidence. Remove gates that show no incremental OOS value; do not add new ones.
4. Collect ≥ several hundred approved OOS outcomes before any probability calibration (Platt first; isotonic only with ample data), with train/validation/final-test splits.
