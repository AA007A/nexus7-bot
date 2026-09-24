"""Durable, ISOLATED forward-evidence store for the AI SHADOW observer.

Evidence authority requires a dedicated PostgreSQL research database:

  * ``EVIDENCE_DATABASE_URL`` must be ``postgresql://`` (never SQLite, never
    the /tmp fallback of bot.database); connection failure refuses startup;
  * ``EVIDENCE_DB_AUTHORITY_ID`` (non-secret) names the evidence database;
  * ``PRODUCTION_DB_AUTHORITY_ID`` and ``PRODUCTION_DB_FINGERPRINT`` pin the
    production execution database; the evidence DB must differ from both and
    from ``DATABASE_URL`` when set;
  * the database must carry (or receive on first start) the marker
    role=AI_SHADOW_EVIDENCE and must NOT contain a ``trades`` table at all.

Tables (every evidence row is bound to ONE immutable window):
  ai_evidence_windows    the prospective window: immutable identity (sealed
                         with identity_sha256) + durable state (status,
                         continuity, boundary progress). At most ONE ACTIVE.
  ai_shadow_candidates   hook candidates (unique candidate_id; payload hash)
  ai_shadow_boundaries   one row per (window, boundary, symbol)
  ai_shadow_heartbeats   per-scan operational records
Every record is sealed with record_sha256 and verified on load. No
credentials are logged: only the endpoint fingerprint and non-secret ids.
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
W_ACTIVE, W_CLOSED, W_INVALID = "ACTIVE", "CLOSED", "INVALID_IDENTITY_CHANGE"
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
TABLES = ("ai_evidence_windows", "ai_shadow_candidates", "ai_shadow_boundaries", "ai_shadow_heartbeats")
# Fields that are observational (wall-clock / latency / lifecycle), not decision-affecting:
# excluded from candidate_payload_sha256.
NON_PAYLOAD_FIELDS = ("observed_at_ms", "decision_latency_ms", "record_sha256", "status", "outcome",
                      "candidate_payload_sha256")

DDL = (
    """CREATE TABLE IF NOT EXISTS ai_evidence_windows (
        pk TEXT PRIMARY KEY, window_id TEXT NOT NULL, status TEXT NOT NULL, sort_key BIGINT,
        record TEXT NOT NULL, updated_at DOUBLE PRECISION)""",
    "CREATE UNIQUE INDEX IF NOT EXISTS ai_evidence_one_active ON ai_evidence_windows ((1)) WHERE status = 'ACTIVE'",
    """CREATE TABLE IF NOT EXISTS ai_shadow_candidates (
        pk TEXT PRIMARY KEY, window_id TEXT NOT NULL REFERENCES ai_evidence_windows(pk), status TEXT NOT NULL,
        sort_key BIGINT, record TEXT NOT NULL, updated_at DOUBLE PRECISION)""",
    """CREATE TABLE IF NOT EXISTS ai_shadow_boundaries (
        pk TEXT PRIMARY KEY, window_id TEXT NOT NULL REFERENCES ai_evidence_windows(pk), status TEXT NOT NULL,
        sort_key BIGINT, record TEXT NOT NULL, updated_at DOUBLE PRECISION)""",
    """CREATE TABLE IF NOT EXISTS ai_shadow_heartbeats (
        pk TEXT PRIMARY KEY, window_id TEXT NOT NULL REFERENCES ai_evidence_windows(pk), status TEXT NOT NULL,
        sort_key BIGINT, record TEXT NOT NULL, updated_at DOUBLE PRECISION)""",
    """CREATE TABLE IF NOT EXISTS ai_evidence_authority (key TEXT PRIMARY KEY, value TEXT)""",
)


class EvidenceStoreRefused(RuntimeError):
    pass


class EvidenceContinuityBroken(RuntimeError):
    pass


class WindowRefused(RuntimeError):
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


def _canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def seal(record: dict) -> dict:
    from bot.ai.runtime import seal as _seal
    return _seal(record)


def verify(pk: str, status: str, record: dict, *, key: str) -> dict:
    from bot.ai.runtime import JournalIntegrityError, record_digest
    if not isinstance(record, dict) or record.get("record_sha256") != record_digest(record):
        raise JournalIntegrityError(f"evidence digest mismatch {pk}")
    if record.get(key) != pk or record.get("status") != status:
        raise JournalIntegrityError(f"evidence identity/status mismatch {pk}")
    return record


def candidate_payload_sha256(record: dict) -> str:
    """Hash of every decision-affecting field (identity, geometry, features,
    model outputs, decision, vetoes, costs, window binding)."""
    body = {k: v for k, v in record.items() if k not in NON_PAYLOAD_FIELDS}
    return hashlib.sha256(_canon(body).encode()).hexdigest()


def window_identity_sha256(identity: dict) -> str:
    return hashlib.sha256(_canon(identity).encode()).hexdigest()


def window_id_for(identity: dict, window_start_ms: int, window_end_ms: int) -> str:
    """window_id = sha256(identity + fixed bounds): a later window with the
    same code identity is still a distinct window."""
    return hashlib.sha256(_canon({"identity_sha256": window_identity_sha256(identity),
                                  "window_start_ms": int(window_start_ms),
                                  "window_end_ms": int(window_end_ms)}).encode()).hexdigest()


# Candidate fields that must equal the window identity (no mixing across windows).
CANDIDATE_IDENTITY_FIELDS = {"code_sha": "code_sha", "bundle_sha256": "bundle_sha256",
                             "policy_sha256": "policy_sha256", "feature_schema_sha256": "feature_schema_sha256",
                             "hook_population": "hook_population", "hook_profile": "hook_profile",
                             "decision_candidate_sha": "code_sha"}


class EvidenceStore:
    """Evidence store over four primitives (``_ins``, ``_get``, ``_upd``,
    ``_list``) + authority metadata. PostgreSQL (asyncpg) here; the in-memory
    subclass is refused for evidence authority unless a test opts in."""

    backend = "postgresql"

    def __init__(self, conn, identity: dict | None = None):
        self.conn = conn
        self.identity = identity or {}
        self.broken = False               # in-process mirror; the durable flag lives in the window row
        self.broken_reason = None

    @classmethod
    async def connect(cls, env, *, read_only: bool = False) -> "EvidenceStore":
        ident = preflight(env)
        try:
            import asyncpg
            conn = await asyncpg.connect(str(env["EVIDENCE_DATABASE_URL"]).replace("postgres://", "postgresql://", 1),
                                         timeout=15)
        except Exception as exc:
            raise EvidenceStoreRefused(f"evidence PostgreSQL unavailable: {type(exc).__name__}") from exc
        if read_only:
            await conn.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
        store = cls(conn, ident)
        await store.init(read_only=read_only)
        return store

    # ── primitives (asyncpg) ─────────────────────────────────────────────
    async def _ins(self, table, pk, window_id, status, sort_key, record_json) -> bool:
        res = await self.conn.execute(
            f"INSERT INTO {table} (pk, window_id, status, sort_key, record, updated_at) "
            "VALUES ($1,$2,$3,$4,$5,$6) ON CONFLICT (pk) DO NOTHING",
            pk, window_id, status, int(sort_key), record_json, time.time())
        return str(res).endswith(" 1")

    async def _get(self, table, pk):
        row = await self.conn.fetchrow(f"SELECT status, record FROM {table} WHERE pk=$1", pk)
        return (row[0], row[1]) if row else None

    async def _upd(self, table, pk, status, record_json, expect_status) -> bool:
        res = await self.conn.execute(f"UPDATE {table} SET status=$2, record=$3, updated_at=$4 "
                                      "WHERE pk=$1 AND status=$5", pk, status, record_json, time.time(),
                                      expect_status)
        return str(res).endswith(" 1")

    async def _list(self, table, *, window_id=None, statuses=None):
        q, args = f"SELECT pk, status, record FROM {table} WHERE TRUE", []
        if window_id is not None:
            args.append(window_id)
            q += f" AND window_id=${len(args)}"
        if statuses:
            args.append(list(statuses))
            q += f" AND status = ANY(${len(args)}::text[])"
        return [tuple(r) for r in await self.conn.fetch(q + " ORDER BY sort_key, pk", *args)]

    async def _meta_get(self, key):
        return await self.conn.fetchval("SELECT value FROM ai_evidence_authority WHERE key=$1", key)

    async def _meta_set(self, key, value):
        await self.conn.execute("INSERT INTO ai_evidence_authority (key, value) VALUES ($1,$2) "
                                "ON CONFLICT (key) DO NOTHING", key, value)

    async def _has_trades_table(self) -> bool:
        return bool(await self.conn.fetchval("SELECT to_regclass('public.trades') IS NOT NULL"))

    async def _ddl(self):
        for sql in DDL:
            await self.conn.execute(sql)

    async def _guard(self, coro, *, write: bool = True):
        """Writes halt once continuity is broken (reads stay possible for
        verification/export). Any DB failure breaks continuity."""
        if write and self.broken:
            coro.close()
            raise EvidenceContinuityBroken(f"evidence continuity broken ({self.broken_reason}); collection halted")
        try:
            return await coro
        except (EvidenceContinuityBroken, WindowRefused):
            raise
        except Exception as exc:
            if type(exc).__name__ in ("JournalIntegrityError", "InvalidJournalTransition"):
                raise
            self.broken, self.broken_reason = True, f"DB_FAILURE:{type(exc).__name__}"
            raise EvidenceContinuityBroken(f"evidence DB failure: {type(exc).__name__}") from exc

    # ── lifecycle ───────────────────────────────────────────────────────
    async def init(self, *, read_only: bool = False) -> dict:
        if self.backend != "postgresql" and not getattr(self, "allow", False):
            raise EvidenceStoreRefused(f"evidence backend {self.backend} is not PostgreSQL")
        if await self._has_trades_table():
            raise EvidenceStoreRefused("database carries the production trading schema (trades table exists)")
        if not read_only:
            await self._ddl()
        try:
            role = await self._meta_get("role")
        except Exception as exc:
            if not read_only:
                raise
            raise EvidenceStoreRefused("not an evidence database (no authority table)") from exc
        if role is None:
            if read_only:
                raise EvidenceStoreRefused("not an evidence database (no role marker)")
            await self._meta_set("role", ROLE)
            await self._meta_set("authority_id", self.identity.get("authority_id"))
            await self._meta_set("fingerprint", self.identity.get("fingerprint"))
        elif role != ROLE:
            raise EvidenceStoreRefused(f"database role is {role}, not {ROLE}")
        elif self.identity.get("authority_id") and \
                await self._meta_get("authority_id") != self.identity.get("authority_id"):
            raise EvidenceStoreRefused("evidence DB authority id changed")
        return {"role": ROLE, **self.identity}

    # ── windows ─────────────────────────────────────────────────────────
    async def _load(self, table, pk, key):
        row = await self._get(table, pk)
        return None if row is None else verify(pk, row[0], json.loads(row[1]), key=key)

    async def windows(self) -> list:
        async def op():
            return [self._verify_window(verify(pk, st, json.loads(r), key="window_id"))
                    for pk, st, r in await self._list("ai_evidence_windows")]
        return await self._guard(op(), write=False)

    @staticmethod
    def _verify_window(w):
        from bot.ai.runtime import JournalIntegrityError
        if window_identity_sha256(w["identity"]) != w["identity_sha256"] or \
                w["window_id"] != window_id_for(w["identity"], w["window_start_ms"], w["window_end_ms"]):
            raise JournalIntegrityError("window identity digest mismatch")
        return w

    async def active_window(self):
        act = [w for w in await self.windows() if w["status"] == W_ACTIVE]
        if len(act) > 1:
            raise WindowRefused("more than one ACTIVE evidence window")
        return act[0] if act else None

    async def create_window(self, identity: dict, *, created_at_ms: int, first_boundary_ms: int,
                            window_end_ms: int, expected_boundaries: int) -> dict:
        """Persist the immutable window BEFORE any candidate. Refuses when an
        ACTIVE window exists (never a silent restart of the statistical clock)."""
        if await self.active_window() is not None:
            raise WindowRefused("an ACTIVE evidence window already exists")
        wid = window_id_for(identity, first_boundary_ms, window_end_ms)
        rec = seal({"window_id": wid, "identity": identity, "identity_sha256": window_identity_sha256(identity),
                    "status": W_ACTIVE,
                    "created_at_ms": int(created_at_ms), "closed_at_ms": None,
                    "window_start_ms": int(first_boundary_ms), "window_end_ms": int(window_end_ms),
                    "first_boundary_ms": int(first_boundary_ms), "last_completed_boundary_ms": None,
                    "expected_boundaries": int(expected_boundaries), "completed_boundaries": 0,
                    "boundary_in_progress_ms": None, "missed_boundaries": 0,
                    "continuity_broken": False, "continuity_reason": None})

        async def op():
            if not await self._ins("ai_evidence_windows", wid, wid, W_ACTIVE, int(first_boundary_ms), _canon(rec)):
                raise WindowRefused("window already exists (identity reuse)")
            return rec
        return await self._guard(op())

    async def update_window(self, window_id: str, /, **state) -> dict:
        """Mutate durable STATE only; the sealed identity can never change."""
        forbidden = {"window_id", "identity", "identity_sha256", "window_start_ms", "window_end_ms",
                     "first_boundary_ms", "expected_boundaries", "created_at_ms"} & set(state)
        if forbidden:
            raise WindowRefused(f"immutable window fields: {sorted(forbidden)}")
        cur = await self._guard(self._load("ai_evidence_windows", window_id, "window_id"), write=False)
        if cur is None:
            raise WindowRefused("unknown window")
        self._verify_window(cur)
        if cur.get("continuity_broken") and state.get("continuity_broken") is False:
            raise WindowRefused("continuity_broken can never be cleared")
        new = seal({**cur, **state})

        async def op():
            if not await self._upd("ai_evidence_windows", window_id, new["status"], _canon(new), cur["status"]):
                raise WindowRefused("concurrent window update")
            return new
        return await self._guard(op())

    async def mark_broken(self, window_id: str, reason: str) -> dict:
        self.broken, self.broken_reason = False, None      # allow the durable write itself
        w = await self.update_window(window_id, continuity_broken=True, continuity_reason=str(reason))
        self.broken, self.broken_reason = True, str(reason)
        return w

    async def window(self, window_id: str) -> dict:
        w = await self._guard(self._load("ai_evidence_windows", window_id, "window_id"), write=False)
        if w is None:
            raise WindowRefused("unknown window")
        return self._verify_window(w)

    async def writable_window(self, window_id: str, ts_ms: int) -> dict:
        """The window accepts rows only while ACTIVE, unbroken and ts inside
        [window_start_ms, window_end_ms)."""
        w = await self.window(window_id)
        if w["status"] != W_ACTIVE:
            raise WindowRefused(f"window is {w['status']}; no append")
        if w.get("continuity_broken"):
            self.broken, self.broken_reason = True, w.get("continuity_reason")
            raise EvidenceContinuityBroken(f"window continuity broken ({w.get('continuity_reason')})")
        if not (w["window_start_ms"] <= int(ts_ms) < w["window_end_ms"]):
            raise WindowRefused("timestamp outside the fixed window bounds")
        return w

    # ── candidates ──────────────────────────────────────────────────────
    async def put_candidate(self, window_id: str, record: dict) -> str:
        """INSERTED | IDEMPOTENT_DUPLICATE. A different payload under an
        existing candidate_id breaks continuity (CANDIDATE_RECONSTRUCTION_MISMATCH).
        The candidate must carry the window's code/bundle/policy/schema/hook
        identity and inherit its cost identity (never changed mid-window)."""
        w = await self.writable_window(window_id, int(record["event_ts"]))
        ident = w["identity"]
        for rk, ik in CANDIDATE_IDENTITY_FIELDS.items():
            if record.get(rk) != ident.get(ik):
                raise WindowRefused(f"candidate {rk} differs from the window identity")
        cost = ident.get("cost_identity") or {}
        a = record.get("assumptions") or {}
        if cost and (a.get("fee_rate") != cost.get("taker_fee")
                     or a.get("slippage_rate") != (cost.get("slippage_rates") or {}).get(record.get("symbol"))):
            await self.mark_broken(window_id, "COST_IDENTITY_MISMATCH")
            raise EvidenceContinuityBroken("COST_IDENTITY_MISMATCH: costs cannot change mid-window")
        rec = {**record, "window_id": window_id}
        rec["candidate_payload_sha256"] = candidate_payload_sha256(rec)
        rec = seal({**rec, "status": PENDING})
        cid = rec["candidate_id"]

        async def op():
            if await self._ins("ai_shadow_candidates", cid, window_id, PENDING, int(rec["event_ts"]), _canon(rec)):
                return "INSERTED"
            return None
        res = await self._guard(op())
        if res:
            return res
        cur = await self.get(cid)
        if cur is not None and cur.get("window_id") == window_id and \
                cur.get("candidate_payload_sha256") == rec["candidate_payload_sha256"]:
            return "IDEMPOTENT_DUPLICATE"
        await self.mark_broken(window_id, "CANDIDATE_RECONSTRUCTION_MISMATCH")
        raise EvidenceContinuityBroken("CANDIDATE_RECONSTRUCTION_MISMATCH")

    async def get(self, candidate_id: str):
        async def op():
            rec = await self._load("ai_shadow_candidates", candidate_id, "candidate_id")
            if rec is not None and rec.get("candidate_payload_sha256") != candidate_payload_sha256(rec):
                from bot.ai.runtime import JournalIntegrityError
                raise JournalIntegrityError("candidate payload hash mismatch")
            return rec
        return await self._guard(op(), write=False)

    async def candidates(self, window_id: str, *statuses) -> list:
        async def op():
            out = []
            for pk, st, raw in await self._list("ai_shadow_candidates", window_id=window_id,
                                                 statuses=statuses or None):
                rec = verify(pk, st, json.loads(raw), key="candidate_id")
                if rec.get("window_id") != window_id or \
                        rec.get("candidate_payload_sha256") != candidate_payload_sha256(rec):
                    from bot.ai.runtime import JournalIntegrityError
                    raise JournalIntegrityError(f"candidate {pk} not bound to window / payload mismatch")
                out.append(rec)
            return out
        return await self._guard(op(), write=False)

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
        return await self._guard(self._upd("ai_shadow_candidates", candidate_id, status, _canon(rec), PENDING))

    # ── boundaries / heartbeats ─────────────────────────────────────────
    async def put_boundary(self, window_id: str, boundary_ms: int, symbol: str, status: str, detail=None) -> str:
        from bot.ai import forward_evidence as fe
        if status not in fe.BOUNDARY_STATUSES:
            raise ValueError(f"unknown boundary status {status}")
        w = await self.writable_window(window_id, int(boundary_ms))
        if symbol not in w["identity"].get("symbol_universe", ()):
            raise WindowRefused(f"symbol {symbol} not in the window universe")
        if (int(boundary_ms) - w["window_start_ms"]) % fe.M15_MS:
            raise WindowRefused("boundary is not a canonical 15m boundary of the window")
        pk = f"{window_id}|{int(boundary_ms)}|{symbol}"
        rec = seal({"boundary_id": pk, "window_id": window_id, "boundary_ms": int(boundary_ms), "symbol": symbol,
                    "status": status, "detail": detail or {}})

        async def op():
            return await self._ins("ai_shadow_boundaries", pk, window_id, status, int(boundary_ms), _canon(rec))
        if await self._guard(op()):
            return "INSERTED"
        cur = await self._guard(self._load("ai_shadow_boundaries", pk, "boundary_id"), write=False)
        if cur is not None and cur["status"] == status and cur.get("detail") == (detail or {}):
            return "IDEMPOTENT_DUPLICATE"
        await self.mark_broken(window_id, "BOUNDARY_RECONSTRUCTION_MISMATCH")
        raise EvidenceContinuityBroken("BOUNDARY_RECONSTRUCTION_MISMATCH")

    async def boundaries(self, window_id: str) -> list:
        async def op():
            out = []
            for pk, st, raw in await self._list("ai_shadow_boundaries", window_id=window_id):
                rec = verify(pk, st, json.loads(raw), key="boundary_id")
                if rec["window_id"] != window_id:
                    from bot.ai.runtime import JournalIntegrityError
                    raise JournalIntegrityError("boundary not bound to window")
                out.append(rec)
            return out
        return await self._guard(op(), write=False)

    async def heartbeat(self, window_id: str, record: dict) -> None:
        await self.window(window_id)
        kind = str(record.get("kind", "SCAN"))
        pk = f"{window_id}|{kind}|{int(record['ts'])}"
        rec = seal({**record, "heartbeat_id": pk, "window_id": window_id, "status": kind})
        await self._guard(self._ins("ai_shadow_heartbeats", pk, window_id, rec["status"], int(record["ts"]),
                                    _canon(rec)))

    async def heartbeats(self, window_id: str) -> list:
        async def op():
            out = []
            for pk, st, raw in await self._list("ai_shadow_heartbeats", window_id=window_id):
                rec = verify(pk, st, json.loads(raw), key="heartbeat_id")
                if rec["window_id"] != window_id:
                    from bot.ai.runtime import JournalIntegrityError
                    raise JournalIntegrityError("heartbeat not bound to window")
                out.append(rec)
            return out
        return await self._guard(op(), write=False)


class MemoryEvidenceStore(EvidenceStore):
    """In-memory primitives for unit tests of the collector logic. Its backend
    is 'memory': ``init()`` REFUSES it for evidence authority unless a test
    explicitly opts in with ``allow_non_durable_for_tests=True``. ``fail``
    simulates a database outage."""

    backend = "memory"

    def __init__(self, *, allow_non_durable_for_tests=False, trades_table=False):
        super().__init__(None, {"authority_id": "test-memory", "fingerprint": "memory"})
        self.t = {t: {} for t in TABLES}
        self.meta = {}
        self.allow = allow_non_durable_for_tests
        self.trades_table = trades_table
        self.fail = False

    def _check(self):
        if self.fail:
            raise ConnectionError("simulated outage")

    async def _ins(self, table, pk, window_id, status, sort_key, record_json):
        self._check()
        if table != "ai_evidence_windows" and window_id not in self.t["ai_evidence_windows"]:
            raise ValueError("foreign key: unknown window")
        if table == "ai_evidence_windows" and status == W_ACTIVE and any(
                s == W_ACTIVE for (_, s, _, _) in self.t[table].values()):
            raise ValueError("unique: one ACTIVE window")
        if pk in self.t[table]:
            return False
        self.t[table][pk] = (window_id, status, int(sort_key), record_json)
        return True

    async def _get(self, table, pk):
        self._check()
        row = self.t[table].get(pk)
        return (row[1], row[3]) if row else None

    async def _upd(self, table, pk, status, record_json, expect_status):
        self._check()
        row = self.t[table].get(pk)
        if row is None or row[1] != expect_status:
            return False
        self.t[table][pk] = (row[0], status, row[2], record_json)
        return True

    async def _list(self, table, *, window_id=None, statuses=None):
        self._check()
        rows = [(pk, s, r, k, w) for pk, (w, s, k, r) in self.t[table].items()
                if (window_id is None or w == window_id) and (not statuses or s in statuses)]
        return [(pk, s, r) for pk, s, r, _, _ in sorted(rows, key=lambda x: (x[3], x[0]))]

    async def _meta_get(self, key):
        self._check()
        return self.meta.get(key)

    async def _meta_set(self, key, value):
        self._check()
        self.meta.setdefault(key, value)

    async def _has_trades_table(self):
        return self.trades_table

    async def _ddl(self):
        self._check()

    async def init(self, *, read_only: bool = False):
        if not self.allow:
            raise EvidenceStoreRefused("non-durable backend refused for evidence")
        return await super().init(read_only=read_only)
