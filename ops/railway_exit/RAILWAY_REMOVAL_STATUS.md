# Railway removal status

Date: 2026-09-25

The user requested that the project be preserved in GitHub only and removed from Railway because the Railway account was banned.

## Result
GitHub preservation completed.

Railway cleanup could not be completed because Railway revoked the required project role. The connector returned:

`You don't have the required role (member) on this resource.`

Therefore:
- no further Railway mutations should be attempted from this archived state;
- old Railway service/project IDs are historical references only;
- the old Railway account must be treated as inaccessible/untrusted;
- future deployment must use a fresh hosting account/project/database;
- secrets from the old hosting account must NOT be copied into GitHub.

## GitHub preservation point
- Archive branch: `archive/railway-exit-20260925`
- Phase 8H collector code, DB schema, deploy files, storage gate, and future Phase 8I spec are preserved.
- Secret-free migration templates are under `ops/railway_exit/`.

## Important safety note
Because the old Railway production runtime could not be administratively removed after access loss, do not assume its state. When a new hosting account is created, first establish fresh infrastructure and fresh credentials. Revoke/rotate old exchange/API/database credentials from their authoritative providers before any new live deployment.
