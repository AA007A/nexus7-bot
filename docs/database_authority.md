# BGX PostgreSQL authority registry

Status captured for BGX-DB-AUTH-001 on 2026-09-21.

## Canonical production authority

- Service: `Postgres`
- Railway service ID: `85019c12-39a2-4ea7-953b-c1dbda898ef8`
- Operational authority ID: `railway-service:85019c12-39a2-4ea7-953b-c1dbda898ef8`
- Sanitized runtime fingerprint: `df6741036a3cab93`
- Database/schema observed by the application: `railway/public`
- Classification: **ACTIVE AUTHORITY**

The runtime fingerprint is computed from the configured PostgreSQL endpoint
without username or password. The production fingerprint matches the Railway
private endpoint for the service above. Credentials are never stored here.

## Other PostgreSQL services

| Service | Railway service ID | Classification |
| --- | --- | --- |
| Postgres-Z2yv | 2274f47a-1a8b-45db-aab2-d9e03c9aff3b | UNKNOWN |
| Postgres-CieO | 4e6e38b2-ad23-4e5f-aca4-acbba3c62453 | UNKNOWN |
| Postgres-xfdH | bb10d131-0856-4308-bd94-18c03343ea8f | UNKNOWN |
| Postgres-I2G0 | 840bcc58-c7cd-4d50-9ba2-757d5c57ba07 | UNKNOWN |

`UNKNOWN` is deliberate. These services are not the `nexus7-bot`
`DATABASE_URL` authority, but that alone does not prove that no other service,
environment, historical workload or recovery path depends on them.

No PostgreSQL service may be deleted solely from this registry.
