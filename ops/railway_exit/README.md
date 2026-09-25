# Railway Exit Snapshot — 2026-09-25

This branch is the GitHub-only preservation point after Railway access was lost/banned.

## Canonical repository
- Repository: `AA007A/nexus7-bot`
- Archive branch: `archive/railway-exit-20260925`
- Archive head at creation: `02298d29990fe16b091bbb3d2f64fff55db910ed`
- Phase 8H collector code is preserved under `alpha_collector/`.
- Railway deployment helpers are preserved under `deploy/alpha_collector/`.
- The predeclared future Phase 8I research spec is preserved under `research/`.

## Last known important code identities
- Production main last verified on Railway: `77ae453e3d01dc269c3b1ee3a4225900c22d9634`
- Frozen Phase 7/8 baseline: `df64030bfe230e4d3e26ad187717e92b624b4661`
- Phase 8H engineering CI baseline: `87b83c1f828efb64e371010cbba799626942e369`
- Phase 8H deploy hardening branch originally deployed at: `cfaa92021972ed2bbb97e1cff9e4e5d82da9351b`
- Final pre-T0 hardening/archive head: `02298d29990fe16b091bbb3d2f64fff55db910ed`

## Frozen Phase 8H identities
- PROSPECTIVE_ALPHA_DATA_CONTRACT_SHA256: `d28ad750e49dc183650cb4e65460d7632666022e38e2f2cfef89aca1dc9a46a7`
- CANONICAL_SCHEMA_SHA256: `faeaf6578d46db1b1453a38d9dd01a016008f50274720d73d9016cf8aca13f2a`
- COLLECTION_EPOCH_SCHEMA_SHA256: `ddadad07457fec8ecdbcdfeed8f2946d87c8d42b7e207b722809dbcf6d39c69e`
- DATABASE_SCHEMA_SHA256: `a0d7695a5f62e10f3bf45f4ee2e55852897c7ad9989089a64860f317dbd55e96`
- Phase 8I prospective research spec SHA256: `452fd34013ca5c67bc653dfa49dc70159c915a92e7a9442fb0c8083a18d42425`

## Safety state at archive time
- No T0 was defined.
- `PHASE8H_EPOCH_V1` had not started.
- Only `PREFLIGHT_ONLY` records had been exercised.
- `PROSPECTIVE_STORAGE_READY=false` was added as a hard fail-closed gate.
- No trading credentials or order authority are required by the collector.
- No secrets are stored in this archive.

## Historical Railway references
These IDs are historical references only and must not be treated as reusable credentials:
- BGX-RESEARCH project: `6951ecba-a5b4-4bf3-b3de-8a6b5ab02730`
- Research environment: `660f1592-a914-48f7-98e3-76948789fffb`
- Phase 8H collector service: `ed294893-9171-4236-aaa0-40993a11aba8`
- Last collector deployment observed: `d7a741de-5932-4883-b0bd-b3b128e02c2a`
- Dedicated research Postgres service: `deb92729-adc4-46f6-9d7f-51c1151fe6da`
- BGX CAPITAL production project: `c3ffa9f5-8c64-4859-a722-a07105ba5e84`
- Production service: `751b41ee-2aef-4487-b19a-f305f15c64fe`
- Last production deployment observed: `5d56867f-7078-4c88-add1-0d39c9318afe`

## New hosting account rule
Do not reuse old Railway references. A future deployment must create fresh project/service/database identities and re-run PREFLIGHT from zero.

Before any future T0:
1. Provision dedicated persistent storage.
2. Set `PROSPECTIVE_STORAGE_READY=true` only after storage verification.
3. Run collector in `PREFLIGHT` only.
4. Confirm DB authority and contract hashes.
5. Confirm public KuCoin REST/WS health and book resync behavior.
6. Explicitly authorize T0 in a separate step.

No automatic migration, live trading, PAPER trading, or prospective epoch start is part of this archive.
