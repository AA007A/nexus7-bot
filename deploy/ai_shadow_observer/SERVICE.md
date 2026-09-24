# Isolated AI SHADOW observer (deploy-ready, not deployed)

Purpose: fresh forward evidence for `FORWARD_SHADOW_EVIDENCE_V1`, with no way to change the exchange.

## Service
- A **separate** Railway service, or a research project. It is never the production trading service.
- Build: the repository `Dockerfile`.
- Start command: `python -m bot.ai.shadow_observer` (see `railway.toml` in this directory).
- It never constructs `TradingEngine`, so it cannot take the LIVE execution lease or touch production positions.

## Environment

| Variable | Value |
|---|---|
| `AI_EXECUTION_MODE` | `SHADOW` (anything else refuses to start) |
| `AI_BUNDLE_DIR` | directory holding the `ai-shadow-observer-bundle` artifact files: `manifest.json`, `classifier.json`, `regressor.json`, `bundle_metadata.json` |
| `AI_BUNDLE_SHA256` | `bundle_sha256` from `bundle_metadata.json` (lifecycle `SHADOW_OBSERVER` or `SHADOW_CHALLENGER`) |
| `SHADOW_SYMBOLS` | exactly the pinned universe: `BTCUSDT ETHUSDT SOLUSDT XRPUSDT ADAUSDT DOGEUSDT LINKUSDT AVAXUSDT DOTUSDT LTCUSDT NEARUSDT ATOMUSDT` (anything else refuses to start) |
| `EVIDENCE_DATABASE_URL` | **secret**, set only in Railway: a **dedicated** PostgreSQL evidence database. SQLite, `/tmp` and the production database are all refused. |
| `EVIDENCE_DB_AUTHORITY_ID` | non-secret id of the evidence database |
| `PRODUCTION_DB_AUTHORITY_ID` / `PRODUCTION_DB_FINGERPRINT` | non-secret pins of the production execution database; the evidence database must differ from both |
| `SHADOW_POLICY_MANIFEST` | optional; defaults to `research/replay_policy_manifest.json` |

Must be **absent**: `KUCOIN_API_KEY`, `KUCOIN_API_SECRET`, `KUCOIN_API_PASSPHRASE`. If any is present, `preflight()` refuses to start. `DATABASE_URL` is not needed; if it is set, it must not point at the evidence database.

## Evidence produced
Each 15m boundary, every `AI_RUNTIME_HOOK_POPULATION_V1` candidate is persisted once in `ai_shadow_candidates`, whether the frozen policy says TRADE or ABSTAIN. Its hypothetical production-parity outcome is resolved later, on closed candles only, and a heartbeat row is written per scan.
- **Censoring:** a gap in the outcome path gives `RIGHT_CENSORED_DATA_GAP`. At window end, open candidates become `RIGHT_CENSORED_DATA_END`; nothing is force-closed.
- **Restart:** every PENDING row is reloaded and digest-verified.
- **DB outage:** evidence continuity is marked broken and collection halts.
- **Artifact:** `bot.ai.forward_artifact.build_forward_shadow_artifact` produces the `FORWARD_SHADOW_EVIDENCE_V2` artifact. The candidate service never returns PASS; the protected evaluator decides.

## Guarantees (tested in `tests/test_ai_phase7c.py`)
- The only exchange client is the public GET-only `PublicKuCoinFuturesClient`. Authenticated calls raise, and `assert_read_only` rejects any client exposing order or cancel methods.
- The observer uses the same hook population (`AI_RUNTIME_HOOK_POPULATION_V1`, LIVE pilot profile), primitives, canonical features, bundle, decision policy and journal as the replay.
- The public maintainMargin MMR proxy is APPROXIMATED, as in the replay.
- At startup it logs `[AI_IDENTITY_OBSERVATION_V1]` with the code, bundle, policy and schema identities.
- Every decision is written to the durable `ai_decisions` table with a sealed `record_sha256`.
