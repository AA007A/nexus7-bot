# NEXUS-7 AI decision authority (Phase 7)

**Status:** the AI decision layer, its safety chain and its research pipeline are implemented and tested offline (`bot/ai/`, `tests/test_ai_decision_authority.py`).

The AI is **not wired into the production engine**. It runs in RESEARCH (in the replay), and the SHADOW and PAPER code paths exist and are tested. LIVE fails closed without Stage-C evidence. Nothing here deploys, changes Railway or sends orders. No positive edge is claimed: see the OOS evidence in the PR comment and `AI_OPERATION_READINESS.json`.

## 1. Audit of the existing NEXUS "AI"
| Component | Location | Classification |
|---|---|---|
| Data validation / freshness | `nexus_ai.validate_data` | RULE_BASED |
| Regime detector | `nexus_ai.detect_regime` | RULE_BASED (ATR/ADX/choppiness thresholds) |
| Multi-timeframe bias | `nexus_ai.analyze_mtf` | RULE_BASED |
| 7 ensemble "models" (trend, momentum, mean reversion, breakout, structure, derivatives, risk/regime) | `nexus_models.py` | HEURISTIC: hand-written indicator rules emitting 0–100 "confidence". Nothing is trained |
| Fusion / conviction | `nexus_ai._fuse` | HEURISTIC weighted vote |
| Win probability | `heuristic_win_probability(confidence)` | HEURISTIC mapping. **Not an empirical probability** |
| Expected value | `nexus_ai.expected_value` | Arithmetic on the heuristic probability |
| Validation utilities | `oos_model_validation.py`, `nexus_oos_inference.py` | STATISTICAL |
| **New** Model A candidates | `bot/ai/models.py` (`LOGISTIC_L2`, `BOOSTED_STUMPS`) | MACHINE_LEARNING |
| **New** Model B candidates | `bot/ai/models.py` (`RIDGE`, `BOOSTED_STUMPS` with squared loss) | MACHINE_LEARNING |
| **New** calibration | `bot/ai/calibration.py` (Platt, fitted on validation) | CALIBRATED_MODEL |
| **New** baseline | `NEXUS_HEURISTIC` (ensemble confidence as a score) | HEURISTIC, benchmark only |

The existing NEXUS ensemble remains available, but only as one input feature (`nexus_confidence`) and as a benchmark. It is not described as trained ML.

## 2. Architecture and hierarchy
```
MARKET DATA (closed candles) → FEATURE ENGINE (bot/ai/features.py, one implementation)
→ REGIME (bot/ai/regime.py) → AI DECISION (bot/ai/decision.py: LONG / SHORT / ABSTAIN)
→ DETERMINISTIC VALIDATION (geometry, RR) → HALTS + OPERATOR KILL SWITCH
→ DRAWDOWN GATE → DAILY STOP → CANONICAL SIZING (risk %, margin, operator cap,
   liquidation, portfolio budget, lot)  [bot/risk_policy.py, unchanged]
→ EXECUTION AUTHORITY (bot/ai/execution_mode.py) → STATE MACHINE (bot/ai/state_machine.py)
→ JOURNAL (bot/ai/journal.py) → reconciliation / performance feedback
```

Invariants, each enforced by a test:
- Every deterministic veto beats every AI output.
- Size comes only from `risk_policy.size_new_entry`. The chain's source never reads confidence or probability, so the AI cannot upsize.
- The AI can raise a halt but never clear one; only an operator with a stated reason can.
- LIVE refuses to start unless `evaluate_live` passed on source-authenticated Stage-C evidence. An environment variable alone is never enough.
- The AI can only recommend exits in SHADOW (`executable = False`). Position management stays deterministic.

## 3. Decision rule
Model A is P(realized net R > 0 after fees, slippage and funding). Model B is E[gross R].

    expected_net_r = E[gross R] − fees_r − slippage_r − funding_r − uncertainty_buffer (0.05 R)

The same deterministic cost charge (round-trip cost fraction ÷ stop distance) is used in training and at decision time. The parity gross R already includes adverse-fill slippage, so this charge double-counts slippage, which is conservative by design.

A trade needs all of the following; otherwise the result is **ABSTAIN**, with reason codes:
- calibrated P ≥ threshold;
- expected_net_r > minimum edge;
- a supported regime;
- an enabled direction;
- fresh data;
- a pinned model hash and feature schema;
- latency within budget.

Thresholds, enabled directions and supported regimes are selected on **validation only**, from small predeclared grids, and frozen before test. SHORT is disabled unless validation shows its own positive edge.

## 4. Training discipline (`bot/ai/training.py`)
- Four purged, embargoed calendar folds, reusing the frozen fold methodology.
- Step 1: TRAIN F1 → VAL F2 → TEST F3. Step 2: TRAIN F1+F2 → VAL F3 → TEST F4.
- The test window never influences features, model choice, hyperparameters, calibration or thresholds (tested by perturbing test outcomes).
- Every window is labelled `HISTORICAL_OOS_PREVIOUSLY_INSPECTED`. This history has been inspected throughout Phases 2–7, so it is not pristine. LIVE promotion additionally requires fresh forward SHADOW/PAPER evidence.
- Retraining is offline only. There is no online learning in LIVE, and no candidate replaces the champion automatically.

## 5. Features (`bot/ai/features.py`)
Each feature declares its name, version, source, timeframe, missing-value policy and causality status. Candles must be closed at decision time, otherwise `FeatureCausalityError` is raised. Nothing is forward-filled.

Funding, open interest and order book are declared `LIVE_ONLY_NO_HISTORICAL_PARITY` and are excluded from model inputs until equivalent history or shadow evidence exists. The replay computes features with the same `compute_features` call used live.

## 6. Model integrity and security
- Artifacts are canonical JSON with a sha256. Pickles and non-JSON payloads are refused, and nothing is executed on load.
- The loaded model's hash must equal the pinned hash (`MODEL_HASH_MISMATCH`), and its feature schema must equal the current one (`FEATURE_SCHEMA_MISMATCH`).
- The model sha256 appears in every decision and in the journal.

## 7. Execution modes, idempotency and resilience
- `EXECUTION_MODE` takes one of RESEARCH, SHADOW, PAPER or LIVE. An unknown value becomes SHADOW, and LIVE fails closed without Stage C.
- SHADOW records intents and cannot send. PAPER uses production costs, lot and tick quantization, minimum-lot rejection and idempotent fills.
- A client order ID is derived deterministically from the decision ID, so a duplicated decision cannot duplicate exposure.
- A timeout or lost ACK leads to reconciliation by client order ID before any resubmit. An unknown reconciliation outcome halts.
- If protection cannot be confirmed after entry, the predeclared fail-safe runs and the machine halts.
- The existing production execution suites remain the authority for the live path:

| Area | Existing suites |
|---|---|
| Durable restart | `test_durable_execution_restart` |
| Partial fills | `test_exec03_partial_fill_reconcile`, `test_durable_partial_exit` |
| Ambiguous orders | `test_exec02_ambiguous_order` |
| Native TPSL | `test_kucoin_native_tpsl` |
| Protection readiness | `test_prelive_protection_failclosed`, `test_protection_readiness_authority` |
| Paper wallet | `paper_*` |

## 8. Exit geometry audit (TP1/TP2)
Production signals carry `tp1 == tp2`, because `Signal.__post_init__` sets both from `tp` and the distinct `calc_sl_tp` path has no caller (OOS_REPORT §C). The AI chain supports distinct TP1 < TP2 (tested). Changing production exit geometry is a strategy change and would require a new production-parity replay, so it is **not** done in this phase.

## 9. Observability and latency
`ShadowRunner.report()` gives:
- decision counts per side;
- orders sent (always 0);
- intents it would have sent;
- latency p50/p95/p99 over the whole path: features → model → chain → intent;
- journal integrity.

Decisions whose information exceeds `max_data_age_ms`, or whose latency exceeds the budget, abstain.

## 10. What is not done
- **Wiring:** the AI is not wired into `engine.py` for SHADOW on live feeds. That changes a production process and is left for the rollout phase.
- **Model:** no model is promoted or pinned for LIVE.
- **Forward evidence:** no forward SHADOW or PAPER evidence exists yet.
- **Dashboards:** dashboard metrics are defined (§9) but are not exported to a UI.
