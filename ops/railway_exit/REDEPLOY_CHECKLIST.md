# Redeploy checklist for a new hosting account

This checklist assumes the old Railway account is no longer trusted/available.

## 1. Create fresh infrastructure
- New hosting project/account.
- New dedicated Postgres instance for research/prospective alpha data.
- Persistent storage: minimum 100 GB, preferred 150 GB initially.
- Do not reuse old Railway IDs, service references, or connection strings.

## 2. Deploy code only from GitHub
Recommended source:
- repository: `AA007A/nexus7-bot`
- branch: `archive/railway-exit-20260925`
- validate against the preserved Phase 8H hashes before use.

Runtime:
- Python 3.11
- start command: `python -m alpha_collector.collector`
- pre-deploy command: `python -m alpha_collector.init_db`

## 3. Secret/config setup
Use `ops/railway_exit/phase8h.env.example` as the variable-name template.
Never commit:
- database passwords/URLs
- exchange private API keys
- trading credentials
- tokens

Collector must start with:
- `COLLECTOR_MODE=PREFLIGHT`
- `PROSPECTIVE_STORAGE_READY=false`
- no T0 authorization
- no trading credentials

## 4. Fresh database authority
Initialize only the new research DB with:
- `BGX_RESEARCH_PROSPECTIVE_ALPHA_V1`
- frozen Phase 8H contract hash

Confirm the new DB fingerprint is different from the new production DB fingerprint.

## 5. PREFLIGHT
Before any prospective epoch:
- REST health passes
- WS execution feed passes
- WS order-book feed passes
- all 12 books reach VALID
- sequence gaps cause resync, not silent continuation
- trade_flow_1m is produced
- timestamps have no unresolved lookahead violations
- only PREFLIGHT_ONLY rows are written
- T0 remains null

Run a multi-hour soak before collection.

## 6. Storage gate
Only after storage is verified:
- set `PROSPECTIVE_STORAGE_READY=true`

This alone does not authorize collection.

## 7. Separate T0 authorization
T0 is a separate operator decision.
Only then may:
- `COLLECTOR_MODE=COLLECT`
- `PROSPECTIVE_T0_AUTHORIZATION` be set
- `PROSPECTIVE_T0_MS` be defined

Never backdate T0.

## 8. Phase 8I
The research plan is already predeclared in:
- `research/phase8i_prospective_alpha_spec_v1.json`

Do not alter it after T0 to fit observed market outcomes.
