# Isolated AI SHADOW observer (deploy-ready, not deployed)

Purpose: fresh forward evidence for `FORWARD_SHADOW_EVIDENCE_V3` (a fixed 30-day prospective window), with no way to change the exchange. V1 and V2 were superseded before any forward window ran (`SUPERSEDED_BEFORE_FIRST_FORWARD_WINDOW`).

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
| `SHADOW_SYMBOLS` | exactly the pinned universe, **in this order**: `BTCUSDT ETHUSDT SOLUSDT XRPUSDT ADAUSDT DOGEUSDT LINKUSDT AVAXUSDT DOTUSDT LTCUSDT NEARUSDT ATOMUSDT` (anything else refuses to start) |
| `EVIDENCE_DATABASE_URL` | **secret**, set only in Railway: a **dedicated** PostgreSQL evidence database. SQLite, `/tmp` and the production database are all refused. |
| `EVIDENCE_DB_AUTHORITY_ID` | non-secret id of the evidence database |
| `PRODUCTION_DB_AUTHORITY_ID` / `PRODUCTION_DB_FINGERPRINT` | non-secret pins of the production execution database; the evidence database must differ from both |
| `SHADOW_POLICY_MANIFEST` | optional; defaults to `research/replay_policy_manifest.json` |
| `AI_EVIDENCE_NEW_WINDOW_AFTER` | normally **unset**. The first window is created automatically only on an empty evidence DB. A later window needs this set to the `window_id` of the latest window (single use; a restart never silently opens a new window). |

`TAKER_FEE` / `BACKTEST_SLIPPAGE` are part of the window cost identity; changing them mid-window invalidates the window.

Must be **absent**: `KUCOIN_API_KEY`, `KUCOIN_API_SECRET`, `KUCOIN_API_PASSPHRASE`. If any is present, `preflight()` refuses to start. `DATABASE_URL` is not needed; if it is set, it must not point at the evidence database.

## Startup sequence (fail closed at each step)
1. no exchange credentials; 2. read-only public client; 3. ordered 12-symbol universe; 4. PostgreSQL connected; 5. evidence-DB isolation verified (no `trades` table at all, role marker, authority id / fingerprint differ from production); 6. bundle loaded and hashed; 7. policy / feature schema verified; 8. contract V3 verified; 9. evidence window created (or the ACTIVE one loaded with an exact identity match) and **committed**; 10. only then is the market observed.

## Evidence window (`ai_evidence_windows`)
- One ACTIVE window per DB (unique index). Identity = contract name/sha, code, bundle, policy, feature schema, hook population/profile, ordered universe + sha, evidence DB id/fingerprint, cost identity (taker fee, slippage model/rates, exit-policy sha, replay-manifest sha). `window_id = sha256(identity, bounds)`.
- `window_start` = first canonical 15m boundary after startup; `window_end = window_start + 30 days` exactly. No extension, no optional stopping, no caller-supplied bounds.
- Restart: the ACTIVE window is loaded; any identity difference marks it `INVALID_IDENTITY_CHANGE` and refuses to append.
- `continuity_broken` is durable in the window row and never cleared (DB failure, interrupted boundary, reconstruction mismatch, cost change).
- At `window_end`: no more candidates; resolver uses information up to `window_end` only; open candidates → `RIGHT_CENSORED_DATA_END`; window `CLOSED`; `[FORWARD_WINDOW_CLOSED]`; the process stops and never starts another window.

## Evidence produced
- `ai_shadow_boundaries`: one row per (window, 15m boundary, symbol) for all 12 symbols — `COMPLETE`, `NO_SIGNAL`, `AI_HOOK`, `NEXUS_REJECTED`, `DATA_MISSING`, `ERROR`, or `MISSED` (boundary not evaluated within 5 min, detected at restart). A missed scan is never "zero candidates".
- Decision candle: the 15m candle with `open_ts == decision_ts` must exist and supplies the ticker open; the last closed 15m/1h/4h bars must be the immediately preceding ones. Otherwise `DATA_MISSING` (`BOUNDARY_INPUT_INCOMPLETE`) and the symbol is not evaluated. No previous-close fallback.
- `ai_shadow_candidates`: every hook candidate once (TRADE and ABSTAIN), bound to the window, with `candidate_payload_sha256`. Same payload → `IDEMPOTENT_DUPLICATE`; different payload → continuity broken (`CANDIDATE_RECONSTRUCTION_MISMATCH`).
- Outcomes resolved on closed candles only (production-parity exits); gaps → `RIGHT_CENSORED_DATA_GAP`.
- `ai_shadow_heartbeats`: one per scan, bound to the window, with the zero-order safety assertions.

## Artifact export (read-only)
`python -m bot.ai.forward_artifact --from-evidence-db --output forward_shadow_evidence.json`
Everything is derived from the DB (bounds, identity, continuity, coverage); digests, identity, boundary uniqueness and heartbeat binding are re-verified; `journal_verified` is computed. The connection is read-only and the output contains no URL or credential. Verdict: `COLLECTING` / `INSUFFICIENT_EVIDENCE` / `BLOCK` only; PASS comes only from the protected evaluator.

## Guarantees (tested in `tests/test_ai_phase7c.py`, `tests/test_ai_phase7d.py`, `tests/test_ai_phase7e.py`)
- The only exchange client is the public GET-only `PublicKuCoinFuturesClient`. Authenticated calls raise, and `assert_read_only` rejects any client exposing order or cancel methods.
- The observer uses the same hook population (`AI_RUNTIME_HOOK_POPULATION_V1`, LIVE pilot profile), primitives, canonical features, bundle, decision policy and journal as the replay.
- The public maintainMargin MMR proxy is APPROXIMATED, as in the replay.
- At startup it logs `[AI_IDENTITY_OBSERVATION_V1]` with the code, bundle, policy and schema identities.
- Every decision is written to the durable `ai_decisions` table with a sealed `record_sha256`.
