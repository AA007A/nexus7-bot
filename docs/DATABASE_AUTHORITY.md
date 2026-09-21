# BGX production database authority

As of 2026-09-21, the canonical durable-state authority designated for the
Railway `production` environment is:

- Railway service: `Postgres`
- Railway service ID: `85019c12-39a2-4ea7-953b-c1dbda898ef8`
- Runtime authority ID:
  `railway-service:85019c12-39a2-4ea7-953b-c1dbda898ef8`
- Expected backend/database/schema: `postgres` / `railway` / `public`

The runtime must expose the same value through `DB_AUTHORITY_ID` and log it
with the sanitized endpoint fingerprint after the database connection is
established. `DATABASE_URL`, credentials, usernames and private hostnames must
never be recorded here or in runtime logs.

## Service classification

| Railway service | Service ID | Classification | Evidence |
| --- | --- | --- | --- |
| `Postgres` | `85019c12-39a2-4ea7-953b-c1dbda898ef8` | ACTIVE AUTHORITY | Explicit authority designation; continuous production checkpoints and material durable-volume growth; only production application with `DATABASE_URL` is `nexus7-bot`. |
| `Postgres-Z2yv` | `2274f47a-1a8b-45db-aab2-d9e03c9aff3b3` | UNKNOWN | No direct reference or data-lineage proof available. |
| `Postgres-CieO` | `4e6e38b2-ad23-4e5f-aca4-acbba3c62453` | UNKNOWN | No direct reference or data-lineage proof available. |
| `Postgres-xfdH` | `bb10d131-0856-4308-bd94-18c03343ea8f` | UNKNOWN | No direct reference or data-lineage proof available. |
| `Postgres-I2G0` | `840bcc58-c7cd-4d50-9ba2-757d5c57ba07` | UNKNOWN | No direct reference or data-lineage proof available. |

`UNKNOWN` is intentional. Lack of observed traffic is not proof that a service
is unused. No PostgreSQL service may be removed, renamed or repurposed until a
separate lineage audit supplies direct evidence and an operator approves the
action.
