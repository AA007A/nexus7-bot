"""Durable, ISOLATED forward-evidence store for the AI SHADOW observer.

Evidence authority requires a dedicated PostgreSQL research database:

  * ``EVIDENCE_DATABASE_URL`` must be ``postgresql://`` (never SQLite, never
    the /tmp fallback of bot.database); connection failure refuses startup;
  * ``EVIDENCE_DB_AUTHORITY_ID`` (non-secret) names the evidence database;
  * ``PRODUCTION_DB_AUTHORITY_ID`` and ``PRODUCTION_DB_FINGERPRINT`` pin the
    production execution database; the evidence DB must differ from both, and
    from ``DATABASE_URL`` when that is set;
  * the database must carry (or receive, on first start) the marker
    role=AI_SHADOW_EVIDENCE, and must not contain production trades.

Rows are sealed with record_sha256 (bot.ai.runtime.record_digest) and status
changes follow FORWARD_TRANSITIONS. No credentials are ever logged: only the
one-way endpoint fingerprint and the non-secret authority ids.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from urllib.parse import urlsplit

ROLE = "AI_SHADOW_EVIDENCE"
PENDING, RESOLVED = "PENDING", "RESOLVED"
CENSORED_END, CENSORED_GAP, INVALID = "RIGHT_CENSORED_DATA_END", "RIGHT_CENSORED_DATA_GAP", "INVALID"
FORWARD_TRANSITIONS = {PENDING: {RESOLVED, CENSORED_END, CENSORED_GAP, INVALID}}
FINAL = {RESOLVED, CENSORED_END, CENSORED_GAP, INVALID}
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")

DDL = (
    """CREATE TABLE IF NOT EXISTS ai_shadow_candidates (
        candidate_id TEXT PRIMARY KEY, decision_id TEXT, event_ts BIGINT, symbol TEXT,
        direction TEXT, status TEXT, bundle_sha256 TEXT, record TEXT, updated_at DOUBLE PRECISION)""",
    "CREATE INDEX IF NOT EXISTS ai_shadow_candidates_status ON ai_shadow_candidates (status)",
    """CREATE TABLE IF NOT EXISTS ai_shadow_heartbeats (
        ts BIGINT PRIMARY KEY, record TEXT)""",
    """CREATE TABLE IF NOT EXISTS ai_evidence_authority (key TEXT PRIMARY KEY, value TEXT)""",
)


class EvidenceStoreRefused(RuntimeError):
    pass


class EvidenceContinuityBroken(RuntimeError):
    pass


def fingerprint(url: str) -> str:
    """One-way endpoint fingerprint (scheme|host|port|path); never credentials."""
    raw = str(url or "").strip()
    if not raw:
        return "UNCONFIGURED"
    p = urlsplit(raw.replace("postgres://", "postgresql://", 1))
    if not p.hostname:
        return "MALFORMED"
    canon = "|".join(((p.scheme or "").lower(), p.hostname.lower(), str(p.port or ""), p.path or ""))
    return hashlib.sha256(canon.encode()).hexdigest()[:16]


def _id(env, key) -> str:
    raw = str(env.get(key, "") or "").strip()
    return raw if _ID_RE.fullmatch(raw) else ("UNSET" if not raw else "INVALID")


def preflight(env) -> dict:
    """Static identity checks BEFORE connecting. Raises EvidenceStoreRefused."""
    url = str(env.get("EVIDENCE_DATABASE_URL", "") or "").strip()
    if not url.startswith(("postgresql://", "postgres://")):
        raise EvidenceStoreRefused("EVIDENCE_DATABASE_URL must be PostgreSQL (no SQLite / tmp fallback)")
    ident = {"authority_id": _id(env, "EVIDENCE_DB_AUTHORITY_ID"), "fingerprint": fingerprint(url),
             "production_authority_id": _id(env, "PRODUCTION_DB_AUTHORITY_ID"),
             "production_fingerprint": str(env.get("PRODUCTION_DB_FINGERPRINT", "") or "").strip() or "UNSET",
             "observer_service": _id(env, "RAILWAY_SERVICE_NAME") if env.get("RAILWAY_SERVICE_NAME") else "UNSET"}
    if ident["authority_id"] in ("UNSET", "INVALID"):
        raise EvidenceStoreRefused("EVIDENCE_DB_AUTHORITY_ID missing or invalid")
    if "UNSET" in (ident["production_authority_id"], ident["production_fingerprint"]) \
            or ident["production_authority_id"] == "INVALID":
        raise EvidenceStoreRefused("production DB identity not pinned (PRODUCTION_DB_AUTHORITY_ID/FINGERPRINT)")
    if ident["authority_id"] == ident["production_authority_id"]:
        raise EvidenceStoreRefused("evidence DB authority id equals the production DB")
    if ident["fingerprint"] in ("MALFORMED", ident["production_fingerprint"]):
        raise EvidenceStoreRefused("evidence DB endpoint equals the production DB (or is malformed)")
    prod_url = str(env.get("DATABASE_URL", "") or "").strip()
    if prod_url and fingerprint(prod_url) == ident["fingerprint"]:
        raise EvidenceStoreRefused("evidence DB endpoint equals DATABASE_URL (production)")
    return ident


def seal(record: dict) -> dict:
    from bot.ai.runtime import seal as _seal
    return _seal(record)


def verify(candidate_id: str, status: str, record: dict) -> dict:
    from bot.ai.runtime import JournalIntegrityError, record_digest
    if not isinstance(record, dict) or record.get("record_sha256") != record_digest(record):
        raise JournalIntegrityError(f"shadow candidate digest mismatch {candidate_id}")
    if record.get("candidate_id") != candidate_id or record.get("status") != status:
        raise JournalIntegrityError(f"shadow candidate identity/status mismatch {candidate_id}")
    return record


class EvidenceStore:
    """PostgreSQL evidence store (asyncpg connection). Subclasses may override
    the primitive ``_q*`` methods (tests); ``backend`` must be postgresql for
    evidence authority."""

    backend = "postgresql"

    def __init__(self, conn, identity: dict | None = None):
        self.conn = conn
        self.identity = identity or {}
        self.broken = False

    @classmethod
    async def connect(cls, env) -> "EvidenceStore":
        ident = preflight(env)
        try:
            import asyncpg
            conn = await asyncpg.connect(str(env["EVIDENCE_DATABASE_URL"]).replace("postgres://", "postgresql://", 1),
                                         timeout=15)
        except Exception as exc:
            raise EvidenceStoreRefused(f"evidence PostgreSQL unavailable: {type(exc).__name__}") from exc
        store = cls(conn, ident)
        await store.init()
        return store

    # ── primitives (asyncpg) ─────────────────────────────────────────────
    async def _qexec(self, sql, *args):
        return await self.conn.execute(sql, *args)

    async def _qfetch(self, sql, *args):
        return [tuple(r) for r in await self.conn.fetch(sql, *args)]

    async def _qval(self, sql, *args):
        return await self.conn.fetchval(sql, *args)

    async def _guard(self, coro):
        if self.broken:
            raise EvidenceContinuityBroken("evidence continuity broken; collection halted")
        try:
            return await coro
        except EvidenceContinuityBroken:
            raise
        except Exception as exc:
            self.broken = True
            raise EvidenceContinuityBroken(f"evidence DB failure: {type(exc).__name__}") from exc

    # ── lifecycle ───────────────────────────────────────────────────────
    async def init(self) -> dict:
        if self.backend != "postgresql":
            raise EvidenceStoreRefused(f"evidence backend {self.backend} is not PostgreSQL")
        for sql in DDL:
            await self._qexec(sql)
        role = await self._qval("SELECT value FROM ai_evidence_authority WHERE key='role'")
        if role is None:
            if await self._qval("SELECT to_regclass('public.trades') IS NOT NULL"):
                if (await self._qval("SELECT count(*) FROM trades") or 0) > 0:
                    raise EvidenceStoreRefused("database contains production trades")
            await self._qexec("INSERT INTO ai_evidence_authority (key, value) VALUES ('role', $1), "
                              "('authority_id', $2), ('fingerprint', $3) ON CONFLICT (key) DO NOTHING",
                              ROLE, self.identity.get("authority_id"), self.identity.get("fingerprint"))
        elif role != ROLE:
            raise EvidenceStoreRefused(f"database role is {role}, not {ROLE}")
        else:
            stored_id = await self._qval("SELECT value FROM ai_evidence_authority WHERE key='authority_id'")
            if self.identity.get("authority_id") and stored_id != self.identity.get("authority_id"):
                raise EvidenceStoreRefused("evidence DB authority id changed")
        return {"role": ROLE, **{k: v for k, v in self.identity.items()}}

    # ── candidates ──────────────────────────────────────────────────────
    async def put_candidate(self, record: dict) -> bool:
        """Insert once; a second insert of the same candidate_id is a no-op."""
        rec = seal({**record, "status": PENDING})
        async def op():
            res = await self._qexec(
                "INSERT INTO ai_shadow_candidates (candidate_id, decision_id, event_ts, symbol, direction, "
                "status, bundle_sha256, record, updated_at) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) "
                "ON CONFLICT (candidate_id) DO NOTHING",
                rec["candidate_id"], rec.get("decision_id"), int(rec["event_ts"]), rec["symbol"],
                rec["direction"], PENDING, rec.get("bundle_sha256"), json.dumps(rec, sort_keys=True), time.time())
            return str(res).endswith(" 1")
        return await self._guard(op())

    async def get(self, candidate_id: str):
        async def op():
            rows = await self._qfetch("SELECT status, record FROM ai_shadow_candidates WHERE candidate_id=$1",
                                      candidate_id)
            if not rows:
                return None
            status, raw = rows[0]
            return verify(candidate_id, status, json.loads(raw))
        return await self._guard(op())

    async def by_status(self, *statuses) -> list:
        async def op():
            rows = await self._qfetch("SELECT candidate_id, status, record FROM ai_shadow_candidates "
                                      "WHERE status = ANY($1::text[]) ORDER BY event_ts, candidate_id",
                                      list(statuses))
            return [verify(cid, st, json.loads(raw)) for cid, st, raw in rows]
        return await self._guard(op())

    async def all_candidates(self) -> list:
        async def op():
            rows = await self._qfetch("SELECT candidate_id, status, record FROM ai_shadow_candidates "
                                      "ORDER BY event_ts, candidate_id")
            return [verify(cid, st, json.loads(raw)) for cid, st, raw in rows]
        return await self._guard(op())

    async def finalize(self, candidate_id: str, status: str, outcome: dict) -> bool:
        """PENDING -> final exactly once (idempotent: a final row is never rewritten)."""
        cur = await self.get(candidate_id)
        if cur is None:
            raise KeyError(candidate_id)
        if cur["status"] in FINAL:
            return False
        if status not in FORWARD_TRANSITIONS[cur["status"]]:
            from bot.ai.runtime import InvalidJournalTransition
            raise InvalidJournalTransition(f"{cur['status']}->{status}")
        rec = seal({**cur, "status": status, "outcome": outcome})
        async def op():
            res = await self._qexec(
                "UPDATE ai_shadow_candidates SET status=$2, record=$3, updated_at=$4 "
                "WHERE candidate_id=$1 AND status=$5", candidate_id, status,
                json.dumps(rec, sort_keys=True), time.time(), PENDING)
            return str(res).endswith(" 1")
        return await self._guard(op())

    # ── heartbeats (uptime, latency, gaps) ─────────────────────────────
    async def heartbeat(self, record: dict) -> None:
        async def op():
            await self._qexec("INSERT INTO ai_shadow_heartbeats (ts, record) VALUES ($1,$2) "
                              "ON CONFLICT (ts) DO NOTHING", int(record["ts"]), json.dumps(record, sort_keys=True))
        await self._guard(op())

    async def heartbeats(self) -> list:
        async def op():
            return [json.loads(r) for (_, r) in await self._qfetch(
                "SELECT ts, record FROM ai_shadow_heartbeats ORDER BY ts")]
        return await self._guard(op())


class MemoryEvidenceStore(EvidenceStore):
    """In-memory store for unit tests of the collector logic. Its backend is
    'memory', so ``init()`` REFUSES it for evidence authority unless the test
    explicitly opts in with ``allow_non_durable_for_tests=True``."""

    backend = "memory"

    def __init__(self, *, allow_non_durable_for_tests=False):
        super().__init__(None, {"authority_id": "test-memory", "fingerprint": "memory"})
        self.rows, self.hb, self.meta = {}, {}, {}
        self.allow = allow_non_durable_for_tests
        self.fail = False

    async def init(self):
        if not self.allow:
            raise EvidenceStoreRefused("non-durable backend refused for evidence")
        return {"role": ROLE}

    async def _check(self):
        if self.fail:
            raise ConnectionError("simulated outage")

    async def put_candidate(self, record):
        async def op():
            await self._check()
            if record["candidate_id"] in self.rows:
                return False
            rec = seal({**record, "status": PENDING})
            self.rows[rec["candidate_id"]] = (PENDING, json.dumps(rec, sort_keys=True))
            return True
        return await self._guard(op())

    async def get(self, candidate_id):
        async def op():
            await self._check()
            if candidate_id not in self.rows:
                return None
            st, raw = self.rows[candidate_id]
            return verify(candidate_id, st, json.loads(raw))
        return await self._guard(op())

    async def by_status(self, *statuses):
        async def op():
            await self._check()
            out = [verify(c, st, json.loads(raw)) for c, (st, raw) in self.rows.items() if st in statuses]
            return sorted(out, key=lambda r: (r["event_ts"], r["candidate_id"]))
        return await self._guard(op())

    async def all_candidates(self):
        return await self.by_status(PENDING, *FINAL)

    async def finalize(self, candidate_id, status, outcome):
        cur = await self.get(candidate_id)
        if cur is None:
            raise KeyError(candidate_id)
        if cur["status"] in FINAL:
            return False
        if status not in FORWARD_TRANSITIONS[cur["status"]]:
            from bot.ai.runtime import InvalidJournalTransition
            raise InvalidJournalTransition(f"{cur['status']}->{status}")
        rec = seal({**cur, "status": status, "outcome": outcome})

        async def op():
            await self._check()
            self.rows[candidate_id] = (status, json.dumps(rec, sort_keys=True))
            return True
        return await self._guard(op())

    async def heartbeat(self, record):
        async def op():
            await self._check()
            self.hb.setdefault(int(record["ts"]), record)
        await self._guard(op())

    async def heartbeats(self):
        return [self.hb[k] for k in sorted(self.hb)]
