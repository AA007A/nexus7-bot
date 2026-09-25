"""Storage for PROSPECTIVE_ALPHA_DATA_V1 (dedicated research Postgres, schema ``alpha_prospective``).

Append-only: every data table rejects UPDATE/DELETE, and INSERT into a SEALED
(table, UTC day, epoch) is rejected by triggers. Idempotency comes from
deterministic unique keys + ON CONFLICT DO NOTHING. Never used against the
production database: startup refuses unless the authority row matches.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
from urllib.parse import urlsplit

from alpha_collector import contract as C

DATA_TABLES = {
    "oi_snapshots": ("source", "symbol", "bucket_ts", "collection_epoch_id"),
    "funding_live_snapshots": ("source", "symbol", "bucket_ts", "collection_epoch_id"),
    "mark_index_snapshots": ("source", "symbol", "bucket_ts", "collection_epoch_id"),
    "trade_events": ("source", "symbol", "venue_trade_id", "collection_epoch_id"),
    "trade_flow_1m": ("source", "symbol", "bucket_ts", "collection_epoch_id"),
    "book_feature_snapshots": ("source", "symbol", "event_ts", "sequence", "collection_epoch_id"),
}
_COMMON = """
    contract_version text NOT NULL, collector_version text NOT NULL, source text NOT NULL, venue text NOT NULL,
    symbol text NOT NULL, metric text NOT NULL, event_ts bigint NOT NULL, observed_ts bigint NOT NULL,
    ingested_ts bigint NOT NULL DEFAULT (extract(epoch from clock_timestamp()) * 1000)::bigint,
    sequence bigint, value numeric, units text NOT NULL, quality_flags text[] NOT NULL DEFAULT '{}',
    source_channel text NOT NULL, raw_payload_sha256 text NOT NULL, collector_instance_id text NOT NULL,
    collection_epoch_id text NOT NULL, utc_day date NOT NULL, payload jsonb NOT NULL DEFAULT '{}'::jsonb"""
_EXTRA = {"oi_snapshots": "bucket_ts bigint NOT NULL", "funding_live_snapshots": "bucket_ts bigint NOT NULL",
          "mark_index_snapshots": "bucket_ts bigint NOT NULL", "trade_events": "venue_trade_id text NOT NULL",
          "trade_flow_1m": "bucket_ts bigint NOT NULL", "book_feature_snapshots": "book_state text NOT NULL"}


def _ddl() -> str:
    s = C.DB_SCHEMA
    parts = [f"CREATE SCHEMA IF NOT EXISTS {s};",
             f"""CREATE TABLE IF NOT EXISTS {s}.authority (id int PRIMARY KEY CHECK (id = 1), authority_id text NOT NULL,
                 contract_sha256 text NOT NULL, schema_version int NOT NULL, created_ts bigint NOT NULL);""",
             f"""CREATE TABLE IF NOT EXISTS {s}.collection_epochs (epoch_id text PRIMARY KEY, manifest jsonb NOT NULL,
                 manifest_sha256 text NOT NULL, t0_ms bigint NOT NULL, created_ts bigint NOT NULL);""",
             f"""CREATE TABLE IF NOT EXISTS {s}.data_gaps (source text NOT NULL, channel text NOT NULL, symbol text NOT NULL,
                 start_ts bigint NOT NULL, end_ts bigint, reason text NOT NULL, collection_epoch_id text NOT NULL,
                 detected_ts bigint NOT NULL, PRIMARY KEY (source, channel, symbol, start_ts, collection_epoch_id));""",
             f"""CREATE TABLE IF NOT EXISTS {s}.collector_heartbeats (collector text NOT NULL, instance_id text NOT NULL,
                 ts bigint NOT NULL, last_source_event_ts bigint, last_persisted_ts bigint, health text NOT NULL,
                 reconnects int NOT NULL, gaps int NOT NULL, collection_epoch_id text NOT NULL, detail jsonb NOT NULL,
                 PRIMARY KEY (collector, instance_id, ts));""",
             f"""CREATE TABLE IF NOT EXISTS {s}.source_health (source text NOT NULL, channel text NOT NULL,
                 symbol text NOT NULL, window_start bigint NOT NULL, window_end bigint NOT NULL, metrics jsonb NOT NULL,
                 collection_epoch_id text NOT NULL, PRIMARY KEY (source, channel, symbol, window_start, collection_epoch_id));""",
             f"""CREATE TABLE IF NOT EXISTS {s}.integrity_manifests (table_name text NOT NULL, utc_day date NOT NULL,
                 collection_epoch_id text NOT NULL, manifest jsonb NOT NULL, digest text NOT NULL, created_ts bigint NOT NULL,
                 PRIMARY KEY (table_name, utc_day, collection_epoch_id));""",
             f"""CREATE TABLE IF NOT EXISTS {s}.sealed_days (table_name text NOT NULL, utc_day date NOT NULL,
                 collection_epoch_id text NOT NULL, digest text NOT NULL, sealed_ts bigint NOT NULL,
                 PRIMARY KEY (table_name, utc_day, collection_epoch_id));""",
             f"""CREATE TABLE IF NOT EXISTS {s}.data_corrections (id bigserial PRIMARY KEY, table_name text NOT NULL,
                 utc_day date NOT NULL, collection_epoch_id text NOT NULL, reason text NOT NULL, correction jsonb NOT NULL,
                 created_ts bigint NOT NULL);""",
             f"""CREATE OR REPLACE FUNCTION {s}.reject_mutation() RETURNS trigger AS $$
                 BEGIN RAISE EXCEPTION 'APPEND_ONLY: % on %.% rejected', TG_OP, TG_TABLE_SCHEMA, TG_TABLE_NAME; END;
                 $$ LANGUAGE plpgsql;""",
             f"""CREATE OR REPLACE FUNCTION {s}.reject_sealed_insert() RETURNS trigger AS $$
                 BEGIN
                   IF EXISTS (SELECT 1 FROM {s}.sealed_days d WHERE d.table_name = TG_TABLE_NAME
                              AND d.utc_day = NEW.utc_day AND d.collection_epoch_id = NEW.collection_epoch_id) THEN
                     RAISE EXCEPTION 'SEALED: % % % is sealed', TG_TABLE_NAME, NEW.utc_day, NEW.collection_epoch_id;
                   END IF;
                   RETURN NEW;
                 END; $$ LANGUAGE plpgsql;"""]
    for t, key in DATA_TABLES.items():
        parts.append(f"CREATE TABLE IF NOT EXISTS {s}.{t} ({_COMMON}, {_EXTRA[t]}, UNIQUE ({', '.join(key)}));")
        parts.append(f"CREATE INDEX IF NOT EXISTS {t}_day_idx ON {s}.{t} (collection_epoch_id, utc_day, symbol);")
        parts.append(f"DROP TRIGGER IF EXISTS {t}_append_only ON {s}.{t};")
        parts.append(f"CREATE TRIGGER {t}_append_only BEFORE UPDATE OR DELETE ON {s}.{t} "
                     f"FOR EACH ROW EXECUTE FUNCTION {s}.reject_mutation();")
        parts.append(f"DROP TRIGGER IF EXISTS {t}_sealed ON {s}.{t};")
        parts.append(f"CREATE TRIGGER {t}_sealed BEFORE INSERT ON {s}.{t} FOR EACH ROW "
                     f"EXECUTE FUNCTION {s}.reject_sealed_insert();")
    for t in ("collection_epochs", "sealed_days", "integrity_manifests", "data_corrections", "authority"):
        parts.append(f"DROP TRIGGER IF EXISTS {t}_append_only ON {s}.{t};")
        parts.append(f"CREATE TRIGGER {t}_append_only BEFORE UPDATE OR DELETE ON {s}.{t} "
                     f"FOR EACH ROW EXECUTE FUNCTION {s}.reject_mutation();")
    return "\n".join(parts)


DDL = _ddl()
DB_SCHEMA_SHA256 = hashlib.sha256(DDL.encode()).hexdigest()


class Refused(RuntimeError):
    """Fail-closed startup / authority / contract refusal."""


class DuplicateEpoch(Refused):
    pass


def url_fingerprint(url: str) -> str:
    u = urlsplit(url)
    return hashlib.sha256(f"{u.scheme}|{u.hostname}|{u.port}|{u.path}".encode()).hexdigest()[:16]


def check_not_production(url: str, production_fingerprint: str | None):
    if not url:
        raise Refused("PROSPECTIVE_DB_URL not set")
    if production_fingerprint and url_fingerprint(url) == production_fingerprint:
        raise Refused("database endpoint fingerprint equals the production fingerprint")


def utc_day(ts_ms: int) -> str:
    return dt.datetime.fromtimestamp(int(ts_ms) / 1000, dt.timezone.utc).date().isoformat()


def row_key(table: str, rec: dict) -> tuple:
    return tuple(rec[k] for k in DATA_TABLES[table])


def row_digest(rec: dict) -> str:
    keep = {k: rec.get(k) for k in C.CANONICAL_FIELDS if k != "ingested_ts"}
    keep["payload"] = rec.get("payload", {})
    return hashlib.sha256(json.dumps(keep, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def manifest_of(table: str, day: str, epoch: str, rows: list, gaps: list) -> dict:
    rows = sorted(rows, key=lambda r: tuple(str(x) for x in row_key(table, r)))
    digests = [row_digest(r) for r in rows]
    flags: dict = {}
    for r in rows:
        for f in r.get("quality_flags") or []:
            flags[f] = flags.get(f, 0) + 1
    by_sym: dict = {}
    for r in rows:
        by_sym[r["symbol"]] = by_sym.get(r["symbol"], 0) + 1
    return {"table": table, "utc_day": day, "epoch_id": epoch, "rows": len(rows), "rows_by_symbol": by_sym,
            "first_event_ts": min((r["event_ts"] for r in rows), default=None),
            "last_event_ts": max((r["event_ts"] for r in rows), default=None), "gaps": len(gaps),
            "quality_flag_counts": flags, "digest": hashlib.sha256("\n".join(digests).encode()).hexdigest()}


# ── in-memory twin (unit tests) ────────────────────────────────────────────
class MemoryStore:
    def __init__(self):
        self.authority = None
        self.tables = {t: {} for t in DATA_TABLES}
        self.epochs, self.gaps, self.heartbeats, self.health = {}, {}, [], {}
        self.manifests, self.sealed, self.duplicates = {}, set(), {t: 0 for t in DATA_TABLES}
        self.clock = lambda: 0

    async def migrate(self):
        return {"schema_sha256": DB_SCHEMA_SHA256}

    async def init_authority(self, authority_id=C.DB_AUTHORITY):
        if self.authority is None:
            self.authority = {"authority_id": authority_id, "contract_sha256": C.CONTRACT_SHA256,
                              "schema_version": C.SCHEMA_VERSION}
        return self.authority

    async def verify_authority(self, expected=C.DB_AUTHORITY):
        a = self.authority
        if not a or a["authority_id"] != expected:
            raise Refused("database authority missing or wrong")
        if a["contract_sha256"] != C.CONTRACT_SHA256 or a["schema_version"] != C.SCHEMA_VERSION:
            raise Refused("database authority contract/schema mismatch")
        return a

    async def register_epoch(self, manifest):
        e = self.epochs.get(manifest["epoch_id"])
        if e is not None:
            if e["manifest_sha256"] != manifest["manifest_sha256"]:
                raise DuplicateEpoch("epoch exists with a different manifest")
            return e
        self.epochs[manifest["epoch_id"]] = manifest
        return manifest

    async def insert(self, table, records, now_ms=None) -> int:
        n = 0
        for r in records:
            r = dict(r, utc_day=utc_day(r["event_ts"]))
            if (table, r["utc_day"], r["collection_epoch_id"]) in self.sealed:
                raise Refused(f"SEALED: {table} {r['utc_day']}")
            k = row_key(table, r)
            if k in self.tables[table]:
                self.duplicates[table] += 1
                continue
            self.tables[table][k] = C.check_ingest(r, now_ms if now_ms is not None else r["observed_ts"])
            n += 1
        return n

    def update(self, table, key, **kw):
        raise Refused("APPEND_ONLY")

    async def gap(self, *, channel, symbol, start_ts, end_ts, reason, epoch):
        k = (C.SOURCE, channel, symbol, int(start_ts), epoch)
        self.gaps.setdefault(k, {"end_ts": end_ts, "reason": reason})
        return True

    async def heartbeat(self, hb):
        self.heartbeats.append(dict(hb))

    async def put_health(self, h):
        self.health[(h["source"], h["channel"], h["symbol"], h["window_start"], h["epoch"])] = h

    async def rows(self, table, *, day=None, epoch=None):
        return [r for r in self.tables[table].values()
                if (day is None or r["utc_day"] == day) and (epoch is None or r["collection_epoch_id"] == epoch)]

    async def seal_day(self, table, day, epoch):
        rows = await self.rows(table, day=day, epoch=epoch)
        gaps = [g for k, g in self.gaps.items() if k[4] == epoch and utc_day(k[3]) == day]
        m = manifest_of(table, day, epoch, rows, gaps)
        self.manifests[(table, day, epoch)] = m
        self.sealed.add((table, day, epoch))
        return m

    async def count(self, table, *, include_preflight=False):
        return sum(1 for r in self.tables[table].values()
                   if include_preflight or r["collection_epoch_id"] != C.PREFLIGHT_EPOCH_ID)


# ── asyncpg implementation ─────────────────────────────────────────────────
class PgStore:
    def __init__(self, conn):
        self.c = conn
        self.s = C.DB_SCHEMA

    @classmethod
    async def connect(cls, url: str, *, production_fingerprint: str | None = None):
        import asyncpg
        check_not_production(url, production_fingerprint)
        try:
            conn = await asyncpg.connect(url, timeout=20)
        except (OSError, asyncio.TimeoutError, asyncpg.PostgresError, asyncpg.InterfaceError, ValueError) as exc:
            raise Refused(f"database connection failed: {type(exc).__name__}") from None
        return cls(conn)

    async def close(self):
        await self.c.close()

    async def migrate(self):
        import asyncpg
        try:
            async with self.c.transaction():
                await self.c.execute(DDL)
        except (asyncpg.PostgresError, asyncpg.InterfaceError) as exc:
            raise Refused(f"migration failed: {type(exc).__name__}") from None
        return {"schema_sha256": DB_SCHEMA_SHA256}

    async def init_authority(self, authority_id=C.DB_AUTHORITY):
        await self.c.execute(f"INSERT INTO {self.s}.authority VALUES (1, $1, $2, $3, $4) ON CONFLICT (id) DO NOTHING",
                             authority_id, C.CONTRACT_SHA256, C.SCHEMA_VERSION, _now())
        return await self.verify_authority(authority_id)

    async def verify_authority(self, expected=C.DB_AUTHORITY):
        import asyncpg
        try:
            r = await self.c.fetchrow(f"SELECT * FROM {self.s}.authority WHERE id = 1")
        except asyncpg.PostgresError:
            r = None
        if not r or r["authority_id"] != expected:
            raise Refused("database authority missing or wrong")
        if r["contract_sha256"] != C.CONTRACT_SHA256 or r["schema_version"] != C.SCHEMA_VERSION:
            raise Refused("database authority contract/schema mismatch")
        return dict(r)

    async def register_epoch(self, manifest):
        r = await self.c.fetchrow(f"SELECT manifest_sha256 FROM {self.s}.collection_epochs WHERE epoch_id = $1",
                                  manifest["epoch_id"])
        if r:
            if r["manifest_sha256"] != manifest["manifest_sha256"]:
                raise DuplicateEpoch("epoch exists with a different manifest")
            return manifest
        await self.c.execute(f"INSERT INTO {self.s}.collection_epochs VALUES ($1, $2::jsonb, $3, $4, $5)",
                             manifest["epoch_id"], json.dumps(manifest, sort_keys=True), manifest["manifest_sha256"],
                             manifest["t0_ms"], _now())
        return manifest

    async def insert(self, table, records, now_ms=None) -> int:
        if not records:
            return 0
        extra = {"oi_snapshots": "bucket_ts", "funding_live_snapshots": "bucket_ts", "mark_index_snapshots": "bucket_ts",
                 "trade_events": "venue_trade_id", "trade_flow_1m": "bucket_ts", "book_feature_snapshots": "book_state"}[table]
        cols = [f for f in C.CANONICAL_FIELDS if f != "ingested_ts"] + ["utc_day", "payload", extra]
        ph = ", ".join(f"${i + 1}" for i in range(len(cols)))
        sql = (f"INSERT INTO {self.s}.{table} ({', '.join(cols)}) VALUES ({ph}) "
               f"ON CONFLICT ({', '.join(DATA_TABLES[table])}) DO NOTHING")
        n = 0
        try:
            async with self.c.transaction():
                for r in records:
                    row = {f: r.get(f) for f in C.CANONICAL_FIELDS if f != "ingested_ts"}
                    row["value"] = None if r.get("value") is None else str(r["value"])
                    row["utc_day"] = dt.date.fromisoformat(utc_day(r["event_ts"]))
                    row["payload"] = json.dumps(r.get("payload", {}), sort_keys=True, default=str)
                    row[extra] = r[extra]
                    st = await self.c.execute(sql, *[row[c] for c in cols])
                    n += int(st.split()[-1])
        except __import__("asyncpg").exceptions.RaiseError as exc:
            if "SEALED" in str(exc):
                raise Refused(f"SEALED: {table}") from None
            raise
        return n

    async def gap(self, *, channel, symbol, start_ts, end_ts, reason, epoch):
        st = await self.c.execute(f"INSERT INTO {self.s}.data_gaps VALUES ($1,$2,$3,$4,$5,$6,$7,$8) ON CONFLICT DO NOTHING",
                                  C.SOURCE, channel, symbol, int(start_ts), None if end_ts is None else int(end_ts),
                                  reason, epoch, _now())
        return st.endswith("1")

    async def heartbeat(self, hb):
        await self.c.execute(f"INSERT INTO {self.s}.collector_heartbeats VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb) "
                             f"ON CONFLICT DO NOTHING", hb["collector"], hb["instance_id"], int(hb["ts"]),
                             hb.get("last_source_event_ts"), hb.get("last_persisted_ts"), hb["health"],
                             int(hb["reconnects"]), int(hb["gaps"]), hb["epoch"], json.dumps(hb.get("detail", {})))

    async def put_health(self, h):
        await self.c.execute(f"INSERT INTO {self.s}.source_health VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7) ON CONFLICT DO NOTHING",
                             h["source"], h["channel"], h["symbol"], int(h["window_start"]), int(h["window_end"]),
                             json.dumps(h["metrics"], sort_keys=True), h["epoch"])

    async def rows(self, table, *, day=None, epoch=None):
        q = f"SELECT * FROM {self.s}.{table} WHERE ($1::date IS NULL OR utc_day = $1::date) AND ($2::text IS NULL OR collection_epoch_id = $2)"
        out = []
        for r in await self.c.fetch(q, dt.date.fromisoformat(day) if day else None, epoch):
            d = dict(r)
            d["value"] = None if d["value"] is None else str(d["value"])
            d["payload"] = json.loads(d["payload"]) if isinstance(d["payload"], str) else d["payload"]
            d["quality_flags"] = list(d["quality_flags"])
            d["utc_day"] = d["utc_day"].isoformat()
            out.append(d)
        return out

    async def seal_day(self, table, day, epoch):
        rows = await self.rows(table, day=day, epoch=epoch)
        gaps = await self.c.fetch(f"SELECT * FROM {self.s}.data_gaps WHERE collection_epoch_id = $1", epoch)
        gaps = [g for g in gaps if utc_day(g["start_ts"]) == day]
        m = manifest_of(table, day, epoch, rows, gaps)
        async with self.c.transaction():
            await self.c.execute(f"INSERT INTO {self.s}.integrity_manifests VALUES ($1,$2,$3,$4::jsonb,$5,$6) ON CONFLICT DO NOTHING",
                                 table, dt.date.fromisoformat(day), epoch, json.dumps(m, sort_keys=True), m["digest"], _now())
            await self.c.execute(f"INSERT INTO {self.s}.sealed_days VALUES ($1,$2,$3,$4,$5) ON CONFLICT DO NOTHING",
                                 table, dt.date.fromisoformat(day), epoch, m["digest"], _now())
        return m

    async def count(self, table, *, include_preflight=False):
        q = f"SELECT count(*) FROM {self.s}.{table}" + ("" if include_preflight else " WHERE collection_epoch_id <> $1")
        return await (self.c.fetchval(q) if include_preflight else self.c.fetchval(q, C.PREFLIGHT_EPOCH_ID))


def _now() -> int:
    import time
    return int(time.time() * 1000)
