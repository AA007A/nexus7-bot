# HWM atomicity test matrix

- PostgreSQL: both key/value updates execute inside one transaction.
- SQLite: BEGIN IMMEDIATE, both updates, one commit.
- SQLite second-write failure: rollback, no commit, strict PersistenceError.
- Database unavailable: strict PersistenceError.
- Duplicate keys: rejected before I/O.
- HWM transition failure: runtime peak/cache does not advance.
- Provenance payload: rejects boolean/non-finite/non-positive numeric values and unsupported reasons.
