# Isolated AI SHADOW observer (deploy-ready, not deployed)

Purpose: fresh forward evidence for `FORWARD_SHADOW_EVIDENCE_V1`, with no way to change the exchange.

## Service
- A **separate** Railway service, or a research project. It is never the production trading service.
- Build: the repository `Dockerfile`.
- Start command: `python -m bot.ai.shadow_observer` (see `railway.toml` in this directory).
- It never constructs `TradingEngine`, so it cannot take the LIVE execution lease or touch production positions.

## Environment (non-secret values only)

| Variable | Value |
|---|---|
| `AI_EXECUTION_MODE` | `SHADOW` (anything else refuses to start) |
| `AI_BUNDLE_DIR` | directory holding `manifest.json`, `classifier.json`, `regressor.json` |
| `AI_BUNDLE_SHA256` | the pinned bundle sha256 (lifecycle `SHADOW_OBSERVER` or `SHADOW_CHALLENGER`) |
| `SHADOW_SYMBOLS` | space-separated production universe |
| `SHADOW_POLICY_MANIFEST` | optional; defaults to `research/replay_policy_manifest.json` |
| `DATABASE_URL` | a **dedicated** evidence database, never the production DB (secret; set only in Railway) |

Must be **absent**: `KUCOIN_API_KEY`, `KUCOIN_API_SECRET`, `KUCOIN_API_PASSPHRASE`. If any is present, `preflight()` refuses to start.

## Guarantees (tested in `tests/test_ai_phase7c.py`)
- The only exchange client is the public GET-only `PublicKuCoinFuturesClient`. Authenticated calls raise, and `assert_read_only` rejects any client exposing order or cancel methods.
- The observer uses the same hook population (`AI_RUNTIME_HOOK_POPULATION_V1`, LIVE pilot profile), primitives, canonical features, bundle, decision policy and journal as the replay.
- The public maintainMargin MMR proxy is APPROXIMATED, as in the replay.
- At startup it logs `[AI_IDENTITY_OBSERVATION_V1]` with the code, bundle, policy and schema identities.
- Every decision is written to the durable `ai_decisions` table with a sealed `record_sha256`.
