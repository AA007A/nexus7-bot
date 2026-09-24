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

## 10. Phase 7B: production integration and model trust

### 10.1 Runtime hierarchy (`bot/ai/runtime.py`, `TradingEngine._open`)

    MARKET DATA → CANONICAL FEATURES → REGIME → AI TRADE/ABSTAIN → DETERMINISTIC GEOMETRY
    → HALT/KILL SWITCH → DRAWDOWN/DAILY STOP → CANONICAL RISK SIZING
    → EXISTING PRODUCTION EXECUTION ENGINE → NATIVE TPSL → POSITION MGMT → RECONCILIATION → JOURNAL

The AI gate runs inside `_open` after the NEXUS approval and before the balance refresh, sizing and dispatch. There is no second execution engine and no new order sender.

When the AI is authoritative, its `decision_id` becomes the engine's idempotency key. `build_client_oid` therefore derives the client order ID from the decision ID. Before `place_order`, the decision-to-client_oid binding is persisted strictly as INTENT_CREATED; after dispatch it moves to SUBMITTED or PENDING_UNKNOWN.

| `AI_EXECUTION_MODE` | Behaviour |
|---|---|
| `OFF` (default) | AI not loaded. Production behaviour is unchanged. |
| `SHADOW` | Full decisions, durably journaled. No authority: never blocks, never authorizes, never mutates the exchange. |
| `PAPER` | Additional mandatory authorization. Requires the PAPER_TRADE engine and a bundle in lifecycle PAPER_CHALLENGER or later. Otherwise HALT. |
| `LIVE` | As PAPER, plus a passing Stage-C gate whose `AI_IDENTITY` stage PASSED (lifecycle LIVE_CHAMPION). Otherwise HALT. No in-candidate Stage-C provider exists, so LIVE always halts here. |

Any other value HALTs.

### 10.2 Model bundle and identity
`AI_MODEL_BUNDLE_V1` is `bundle_sha256` over the following:
- the classifier and regressor artifact sha256;
- calibration;
- the decision policy and its sha256;
- the feature schema version and sha256;
- the AI version;
- the training code SHA;
- the training dataset manifest sha256;
- the training period;
- selection evidence;
- created_at;
- the lifecycle state.

`ModelBundle.load` verifies all of the above against the pinned `AI_BUNDLE_SHA256` (files in `AI_BUNDLE_DIR`). Any mismatch at startup raises HALT `MODEL_ARTIFACT_MISMATCH` before any entry.

`decision_id = f(symbol, 15m event ts, direction, feature hash, feature schema sha, bundle sha, policy sha, candidate code sha)`.

### 10.3 Canonical cost contract

    predicted_net_r = predicted_gross_r − fees_r − CONSERVATIVE_SLIPPAGE_BUFFER_r − funding_r − decision_uncertainty_buffer_r

At runtime the training cost (`cost_fraction / stop_distance`) is split into `fees_r` (2 × taker ÷ stop distance) and `CONSERVATIVE_SLIPPAGE_BUFFER_r` (the remainder). The sum is identical to training. Funding is 0 at decision time in both.

### 10.4 Fail-closed selection and calibration
- Thresholds are chosen on VALIDATION.
- Each direction and regime is then re-evaluated at those final thresholds and marked ENABLED, DISABLED_NEGATIVE (mean ≤ 0) or DISABLED_INSUFFICIENT_EVIDENCE (n < 30).
- There is no LONG fallback. If nothing is enabled, the policy is ABSTAIN_ALL. UNKNOWN and EXTREME are never tradable.
- If the calibrated probability does not beat the base-rate Brier score, `probability_authorizes = false`, and every decision carries the veto `CALIBRATION_NOT_AUTHORIZED`.
- Every test fold reports Brier, base-rate Brier, log loss, ECE and a reliability curve.

### 10.5 `AI_RESEARCH_PROMOTION_GATE` (`bot/ai/ai_gate.py`)
The gate is an independent verifier. It never trusts stored status, stored CIs or a stored ACF. It re-derives the required block length and recomputes AI-approved expectancy and AI uplift authority from the retained influence aggregates, using the frozen methodology.

It fails closed on each of the following:
- status and data label;
- purged-window ordering;
- fewer than 100 approved samples;
- CI lower bound ≤ 0;
- invalid residual dependence;
- calibration that does not beat the base rate on every TEST fold;
- symbol or period dominance, or too few symbols;
- cost stress (FEES_PLUS_50PCT, SLIPPAGE_X2, COMBINED ≤ 0);
- the per-TEST-fold portfolio: each fold starts from the same equity, and every fold must end above its start with realized return > 0, MDD within the limit and immaterial censoring.

TRAIN and VALIDATION are never promotion evidence.

### 10.6 Lifecycle (`bot/ai/lifecycle.py`)

| Step | Requires |
|---|---|
| RESEARCH_CANDIDATE → SHADOW_CHALLENGER | gate PASS + MODEL_SELECTION_STABLE |
| SHADOW_CHALLENGER → PAPER_CHALLENGER | FORWARD_SHADOW_EVIDENCE |
| PAPER_CHALLENGER → LIVE_CHAMPION | FORWARD_PAPER_EVIDENCE + Stage-C code + AI identity + human approval |

The replay can create at most a SHADOW_CHALLENGER, and only when the gate passes and selection is stable. Forward contracts are predeclared in `bot/ai/forward_evidence.py` and hashed, so a change restarts the window.

### 10.7 Stage-C AI identity (`bot/ai/identity.py`)
At startup, when the mode is not OFF, the runtime logs `[AI_IDENTITY_OBSERVATION_V1]`. The line carries the code sha, deployment, mode, AI version, bundle sha, policy sha, feature schema sha and an integrity digest. It contains no weights, thresholds or secrets.

`verify_ai_identity` requires three things to agree on code, bundle, policy, schema and AI version:
- the envelope `ai_identity` claim;
- the latest observation in that deployment's logs (mode LIVE, fresh);
- the identity approved by the protected environment (`approved_ai_identity`, lifecycle LIVE_CHAMPION).

A missing, unknown, changed, stale or unapproved identity is a BLOCK. `evaluate_live` reports the result as `stages.AI_IDENTITY`. The NEXUS-only Stage-C verdict is unchanged.

### 10.8 Halt authority, journal and restart
- `AutonomousStateMachine` enters HALT only through `halt()`, which always registers a `HaltController` condition. An illegal transition halts with `ILLEGAL_STATE_TRANSITION`.
- HALT is left only by `operator_recover(actor=OPERATOR, reason, checks)`. The checks exchange_reconciled, positions_protected, model_verified and clock_ok must all be true. The recovery is journaled.
- Durable journal: the `ai_decisions` table in the existing database layer (PostgreSQL, with SQLite fallback). There is one row per decision_id, with a unique client_oid, the status, the bundle and policy sha, and the sanitized record: decision, costs, feature snapshot and record sha256. Authoritative modes never trade on an unjournaled decision.
- Restart: every INTENT_CREATED, SUBMITTED or PENDING_UNKNOWN decision is reconciled by client_oid before any new AI-authorized entry. An unknown result halts with RECONCILIATION_UNCERTAIN. Nothing is resubmitted, and the same 15m market event is never re-decided into a new order (`DUPLICATE_MARKET_EVENT`).
- Parity: a golden test pins replay-versus-runtime model input, feature hash and regime, with the forming candle removed by `closed_only`.

## 11. Phase 7C: runtime hook parity and evidence lock

### 11.1 `AI_RUNTIME_HOOK_POPULATION_V1` (`bot/ai/hook.py`)
The AI trains and is tested **only** on candidates that reach `TradingEngine._ai_gate(sig, nx_dec)`, carrying exactly the state the engine holds at that point.

A candidate is `ai_hook_eligible` only if every step below passes:
1. The scan funnel passes: session-adjusted score of at least the minimum, the regime allows the direction, and expected PnL is above 0.
2. The initial NEXUS decision approves.
3. In the LIVE pilot profile only, CROSS geometry is not BLOCK. When the geometry is ADJUSTED, the NEXUS recheck also approves.

The engine sets `sig.score` to the session-adjusted score before `_open`. In the LIVE pilot profile, the CROSS wrapper around `_nexus_validate` then changes `sig.sl`, `sig.tp` and `sig.rr` to the compressed geometry and returns the recheck decision. The hook observation therefore carries:
- the session-adjusted score;
- the compressed stop and its RR;
- the confidence of the decision that authorized that geometry.

The previous replay features mixed the compressed stop with the original RR, the raw score and the initial confidence, which was a defect. They also used 80-candle windows where the engine used 200.

| Profile | When | Hook geometry |
|---|---|---|
| `LIVE_PILOT_POST_CROSS_GEOMETRY` (training profile) | `paper_trade` off and pilot enabled | compressed |
| `PAPER_OR_UNPILOTED_PRE_GEOMETRY` | otherwise | original |

A bundle binds `hook_population` and `hook_profile`:
- **Authoritative modes:** a profile mismatch halts with `AI_HOOK_PROFILE_MISMATCH`.
- **SHADOW:** the decision is journaled as `SHADOW_PROFILE_MISMATCH`.

Consequence: a PAPER_TRADE engine cannot produce LIVE-profile evidence (see the PAPER forward contract).

Features use the canonical closed windows (`FEATURE_WINDOWS` = 80 / 50 / 30), feature schema `NEXUS7_AI_FEATURES_V3`. The engine reuses the exact windows its `_nexus_validate` fetched.

A golden end-to-end test covers five cases, comparing every hook field and the features, missing flags, hash and regime between the engine and the replay:
- the real bootstrap-wrapped `_nexus_validate` followed by the real `_ai_gate`;
- the replay reconstruction of the same candidate;
- the compressed-stop case;
- the pre-geometry case;
- the NEXUS-rejected and recheck-rejected cases, where neither path reaches the hook.

The replay reports hook-population counts in `population_counts.ai_hook_population`:
- `strategy_candidates`, `executable_candidates` and `initial_nexus_approved`;
- `ai_hook_eligible` and `post_ai_downstream_eligible`;
- `production_approved`;
- a count per stopping stage.

### 11.2 Effective-execution parity
Downstream of the hook, the engine still runs the legacy `scoring.calculate` pre-score, pilot guards, pre-dispatch spread and depth, and market-risk feeds. The replay reproduces none of these, and historical order flow, macro and news are not fabricated. So `AI_EFFECTIVE_EXECUTION_PARITY = INCOMPLETE`.

Historical AI results are `AI_HOOK_EDGE` only. They can justify a zero-order SHADOW observation. `ai_gate.live_historical_promotion` blocks while parity is incomplete. AI LIVE (Stage C `AI_LIVE`) and LIVE_CHAMPION promotion require `AI_EFFECTIVE_EXECUTION_EDGE`.

### 11.3 `AI_AGGREGATE_BLOCK_BOOTSTRAP_V1` (`bot/ai/aggregate_bootstrap.py`)
The AI gate rebuilds every bootstrap draw from the retained per-block aggregates at every authoritative length:
- **Mean:** Σ sampled `sum_r` ÷ Σ sampled `count`.
- **Uplift:** Σ `a_sum`/Σ `a_count` − Σ `b_sum`/Σ `b_count`.

It uses the pinned seed (11), the pinned sample count (1000), the same block universe and the 2.5/97.5 percentiles. It also recomputes the point estimate, the influence-score residual ACF and the authority interval. A deleted interval, a different sample count or a zero-variance series blocks.

Stored CIs, status and ACF are comparison fields only. On real replay aggregates the recomputed CIs equal the stored ones exactly. A forged positive stored CI cannot create a PASS, and a forged negative one cannot destroy a true PASS.

### 11.4 Stage C: `release_authority_kind`
- `evaluate_live(..., release_authority_kind="NEXUS_ONLY" | "AI_LIVE")`.
- **AI_LIVE:** `AI_IDENTITY == PASS` and AI effective-execution evidence are part of `live_ok`. A result of PASS together with `AI_IDENTITY = BLOCK` is impossible.
- **Approval timing:** the human approval must post-date the authenticated `AI_IDENTITY_OBSERVATION_V1`, and the approval digest binds `ai_identity`.
- **NEXUS_ONLY:** unchanged. An envelope carrying `ai_identity` and evaluated as NEXUS_ONLY is `RELEASE_AUTHORITY_KIND_AMBIGUOUS`.
- **Runtime:** requires `release_authority_kind == AI_LIVE`.

### 11.5 Journal integrity and transitions
- **Sealing:** `record_sha256 = record_digest(record)`, which excludes itself, is recomputed on every write.
- **Verification:** every critical load (restart, reconciliation, duplicate detection, order binding) checks the digest, the decision_id, the status column and the client_oid column. Any mismatch halts with `AI_JOURNAL_INTEGRITY_FAILURE` and no trade happens.
- **Transitions:** only these moves are allowed. Anything else, including backwards moves, fails closed.

  | From | Allowed next statuses |
  |---|---|
  | ABSTAIN | none (terminal) |
  | APPROVED | INTENT_CREATED, DOWNSTREAM_BLOCKED |
  | INTENT_CREATED | SUBMITTED, PENDING_UNKNOWN, RECONCILED_FOUND, RECONCILED_NOT_FOUND |
  | SUBMITTED | FILLED, PENDING_UNKNOWN, RECONCILED_FOUND, RECONCILED_NOT_FOUND |
  | PENDING_UNKNOWN | PENDING_UNKNOWN, RECONCILED_FOUND, RECONCILED_NOT_FOUND |
  | RECONCILED_FOUND | FILLED, CLOSED |
  | FILLED | CLOSED |

- **SHADOW statuses:** all terminal. A SHADOW event is never rewritten.

### 11.6 Selection stability
Stability requires the same classifier, the same regressor and an identical `decision_policy_sha256` in every step. The policy hash covers thresholds, directions, supported regimes, `probability_authorizes`, the buffer, freshness and latency.

### 11.7 SHADOW semantics and the isolated observer
In the production engine, `AI_EXECUTION_MODE=SHADOW` only means the AI has no order authority; the legacy engine can still trade. Forward SHADOW evidence therefore comes from `bot/ai/shadow_observer.py`, deploy-ready in `deploy/ai_shadow_observer/` and not deployed. The observer:
- never constructs `TradingEngine` and never takes the execution lease;
- uses only the GET-only public client;
- refuses exchange credentials and any mode other than SHADOW;
- rebuilds the LIVE-pilot hook with the replay's primitives;
- journals durably.

When the gate blocks, the replay builds a zero-order `SHADOW_OBSERVER` bundle: no edge claim, no order authority, no LIVE authority, never promotable.

## 12. What is not done
- **Deployment:** nothing is deployed, and no Railway variable has changed. `AI_EXECUTION_MODE` stays unset (OFF) in production.
- **LIVE:** no LIVE_CHAMPION exists, and no trusted Stage-C or AI-identity provider is implemented in-candidate.
- **Forward evidence:** no forward SHADOW or PAPER evidence exists yet.
- **Dashboards:** dashboard metrics are defined (§9) but are not exported to a UI.
