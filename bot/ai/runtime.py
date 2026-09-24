"""AI runtime integration: the AI as a PRE-TRADE authority inside the EXISTING
production path (bot.engine.TradingEngine._open). No second execution engine
and no order sender live here.

    MARKET DATA -> CANONICAL FEATURES -> REGIME -> AI TRADE/ABSTAIN (this module)
    -> DETERMINISTIC GEOMETRY -> HALT/KILL -> DRAWDOWN/DAILY STOP -> CANONICAL
    SIZING -> EXISTING EXECUTION ENGINE -> NATIVE TPSL -> POSITION MGMT
    -> RECONCILIATION -> JOURNAL

AI_EXECUTION_MODE (environment):
  OFF (default)  the AI is not loaded; production behaviour is byte-for-byte
                 unchanged.
  SHADOW         full AI decisions on every NEXUS-approved candidate, durably
                 journaled; the AI has NO authority (never blocks, never
                 authorizes) and this module can never mutate the exchange.
  PAPER          the AI is an ADDITIONAL mandatory authorization; requires the
                 engine to run PAPER_TRADE (simulated fills). A live engine in
                 PAPER mode HALTs.
  LIVE           as PAPER, on the real engine, and additionally requires a
                 passing Stage-C LIVE_RELEASE_GATE whose AI identity binding
                 (code sha + bundle sha + policy sha + feature schema sha + AI
                 version, lifecycle LIVE_CHAMPION) PASSED. Otherwise HALT.

The AI can only say "no" or "this opportunity": it never sizes, never upsizes,
never overrides drawdown / daily stop / liquidation / Stage C, never clears a
halt and never bypasses reconciliation. When authoritative, the decision_id
becomes the engine's idempotency key, so client_oid derives from decision_id
and a repeated decision can never become a repeated order.

Startup: any bundle problem (missing, hash, pin, schema, AI version, policy)
raises HALT MODEL_ARTIFACT_MISMATCH before any entry. Restart: every durable
decision whose order is not final is reconciled by client_oid BEFORE any new
AI-authorized entry; the same market event is never re-decided or re-ordered.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from bot.ai import decision as dec
from bot.ai import features as fx
from bot.ai import identity as ai_identity
from bot.ai import lifecycle
from bot.ai.halt import HaltController

MODES = ("OFF", "SHADOW", "PAPER", "LIVE")
AUTHORITATIVE = ("PAPER", "LIVE")
EVENT_MS = fx.TF_MS["15m"]            # a market event = one 15m decision boundary (as in the replay)
FINAL_AFTER_RECONCILE = {"FOUND": "RECONCILED_FOUND", "NOT_FOUND": "RECONCILED_NOT_FOUND"}


class AIRuntimeError(RuntimeError):
    pass


def resolve_ai_mode(env) -> str:
    raw = str((env or {}).get("AI_EXECUTION_MODE", "OFF") or "OFF").strip().upper()
    return raw if raw in MODES else "INVALID"


def runtime_costs(*, cost_fraction: float, stop_distance_pct: float, taker_fee: float) -> dict:
    """Split the SAME decision-time cost used in training
    (training.cost_estimate_r = cost_fraction / stop_distance_pct) into the
    named cost-contract quantities. Funding is 0 at decision time in both."""
    if not stop_distance_pct or stop_distance_pct <= 0:
        return {"fees_r": 1e9, "slippage_r": 0.0, "funding_r": 0.0}
    total = float(cost_fraction) / float(stop_distance_pct)
    fees = min(total, 2.0 * float(taker_fee) / float(stop_distance_pct))
    return {"fees_r": fees, "slippage_r": max(0.0, total - fees), "funding_r": 0.0}


def authorize_ai_live(stage_c_result) -> list[str]:
    """[] when AI LIVE is authorized; otherwise the blockers."""
    d = stage_c_result.to_dict() if hasattr(stage_c_result, "to_dict") else (stage_c_result or {})
    b = []
    if not (d.get("gate") == "LIVE_RELEASE_GATE" and d.get("verdict") == "PASS"
            and d.get("live_provenance_authenticated") is True and d.get("production_ready") is True):
        b.append("STAGE_C_LIVE_RELEASE_GATE_NOT_PASSED")
    if (d.get("stages") or {}).get("AI_IDENTITY") != "PASS":
        b.append("STAGE_C_AI_IDENTITY_NOT_PASSED")
    if (d.get("stages") or {}).get("release_authority_kind") != "AI_LIVE":
        b.append("STAGE_C_NOT_AI_LIVE_RELEASE")
    return b


def load_bundle(env) -> dec.ModelBundle:
    """Bundle from AI_BUNDLE_DIR (manifest.json, classifier.json, regressor.json)
    pinned by AI_BUNDLE_SHA256. Raises on ANY mismatch."""
    d, pin = env.get("AI_BUNDLE_DIR"), env.get("AI_BUNDLE_SHA256")
    if not d or not pin:
        raise AIRuntimeError("AI_BUNDLE_DIR / AI_BUNDLE_SHA256 not configured")
    p = Path(d)
    try:
        man = (p / "manifest.json").read_text(encoding="utf-8")
        clf = (p / "classifier.json").read_text(encoding="utf-8")
        reg = (p / "regressor.json").read_text(encoding="utf-8")
    except OSError as exc:
        raise AIRuntimeError(f"bundle files unreadable: {type(exc).__name__}") from exc
    return dec.ModelBundle.load(man, clf, reg, pinned_bundle_sha=str(pin).strip().lower())


# ── durable journal: digest + status transitions ─────────────────────────
AUTH_TRANSITIONS = {
    "ABSTAIN": set(),
    "APPROVED": {"INTENT_CREATED", "DOWNSTREAM_BLOCKED"},
    "INTENT_CREATED": {"SUBMITTED", "PENDING_UNKNOWN", "RECONCILED_FOUND", "RECONCILED_NOT_FOUND"},
    "SUBMITTED": {"PENDING_UNKNOWN", "RECONCILED_FOUND", "RECONCILED_NOT_FOUND", "FILLED"},
    "PENDING_UNKNOWN": {"PENDING_UNKNOWN", "RECONCILED_FOUND", "RECONCILED_NOT_FOUND"},
    "RECONCILED_FOUND": {"FILLED", "CLOSED"},
    "FILLED": {"CLOSED"},
    "RECONCILED_NOT_FOUND": set(),
    "DOWNSTREAM_BLOCKED": set(),
    "CLOSED": set(),
}
AUTH_INITIAL = {"ABSTAIN", "APPROVED"}
SHADOW_STATUSES = {"SHADOW_TRADE", "SHADOW_ABSTAIN", "SHADOW_PROFILE_MISMATCH"}   # all terminal


class JournalIntegrityError(RuntimeError):
    pass


class InvalidJournalTransition(RuntimeError):
    pass


def record_digest(record: dict) -> str:
    """Canonical sha256 of a journal record, excluding record_sha256 itself."""
    body = {k: v for k, v in record.items() if k != "record_sha256"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"),
                                     default=str).encode()).hexdigest()


def seal(record: dict) -> dict:
    rec = {k: v for k, v in record.items() if k != "record_sha256"}
    rec["record_sha256"] = record_digest(rec)
    return rec


def verify_record(decision_id: str, status: str | None, record: dict) -> dict:
    """Digest, identity and status-column consistency. Raises on any mismatch."""
    if not isinstance(record, dict) or record.get("record_sha256") != record_digest(record):
        raise JournalIntegrityError(f"record digest mismatch for {decision_id}")
    if (record.get("decision") or {}).get("decision_id") != decision_id:
        raise JournalIntegrityError(f"decision_id mismatch for {decision_id}")
    if status is not None and record.get("status") != status:
        raise JournalIntegrityError(f"status column differs from record for {decision_id}")
    return record


def check_transition(old: str, new: str) -> None:
    if old in SHADOW_STATUSES or new in SHADOW_STATUSES:
        raise InvalidJournalTransition(f"{old}->{new}: SHADOW records are terminal")
    if new not in AUTH_TRANSITIONS.get(old, set()):
        raise InvalidJournalTransition(f"{old}->{new}")


def _record(d: dec.AIDecision, *, mode: str, features: fx.FeatureVector | None, extra: dict | None) -> dict:
    """Sanitized durable record: identities, outputs, feature snapshot. No
    weights, secrets or URLs."""
    body = {"mode": mode, "decision": d.to_dict(),
            "features": ({"values": features.values, "missing": list(features.missing),
                          "decision_ts": features.decision_ts,
                          "newest_candle_close_ts": features.newest_candle_close_ts}
                         if features is not None else None),
            **(extra or {})}
    return seal(body)


class DurableAIJournal:
    """AI decisions persisted in the existing durable layer (bot.database:
    PostgreSQL, SQLite fallback). One row per decision_id; client_oid unique.
    Every write re-seals record_sha256; every load verifies it."""

    def __init__(self, db=None):
        if db is None:
            from bot import database as db
        self.db = db

    async def put(self, d: dec.AIDecision, status: str, *, mode: str, client_oid=None,
                  features=None, extra=None, strict=True) -> bool:
        if status not in AUTH_INITIAL | SHADOW_STATUSES:
            raise InvalidJournalTransition(f"invalid initial status {status}")
        if await self.db.load_ai_decision(d.decision_id, strict=strict) is not None:
            if status in SHADOW_STATUSES:
                return False                      # same SHADOW event already journaled
            raise InvalidJournalTransition(f"decision {d.decision_id} already journaled")
        rec = _record(d, mode=mode, features=features, extra={**(extra or {}), "status": status,
                                                                 "client_oid": client_oid})
        return await self.db.save_ai_decision(d.decision_id, client_oid, int(d.timestamp), d.symbol,
                                              d.direction_proposed, status, d.model_sha256,
                                              d.policy_sha256, json.dumps(rec, sort_keys=True, default=str),
                                              strict=strict)

    async def set_status(self, decision_id: str, status: str, *, strict=True, **extra) -> bool:
        row = await self.get(decision_id)
        if row is None:
            if strict:
                raise AIRuntimeError("unknown decision_id")
            return False
        old, rec = row
        check_transition(old, status)
        rec = seal({**rec, **extra, "status": status})
        d = rec["decision"]
        return await self.db.save_ai_decision(decision_id, rec.get("client_oid"), int(d["timestamp"]),
                                              d["symbol"], d["direction_proposed"], status,
                                              d.get("model_sha256"), d.get("policy_sha256"),
                                              json.dumps(rec, sort_keys=True, default=str), strict=strict)

    async def get(self, decision_id: str):
        row = await self.db.load_ai_decision(decision_id)
        if row is None:
            return None
        status, rec = row
        return status, verify_record(decision_id, status, rec)

    async def open_decisions(self) -> list:
        rows = await self.db.load_open_ai_decisions()
        for r in rows:
            verify_record(r["decision_id"], r["status"], r["record"])
            if r.get("client_oid") != r["record"].get("client_oid"):
                raise JournalIntegrityError(f"client_oid column differs for {r['decision_id']}")
        return rows


@dataclass
class GateOutcome:
    allow: bool
    authoritative: bool
    reason: str
    decision: dec.AIDecision | None = None


class AIRuntime:
    def __init__(self, env=None, *, journal: DurableAIJournal | None = None, paper_trade: bool = False,
                 clock_ms=None, log=None):
        self.env = dict(os.environ if env is None else env)
        self.mode = resolve_ai_mode(self.env)
        self.halts = HaltController()
        self.journal = journal
        self.paper_trade = bool(paper_trade)
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self.log = log
        self.bundle = None
        self.authority = None
        self.recovered = False
        self.started = False
        self.live_blockers: list[str] = []
        self._events: set = set()
        self.hook_profile = None

    @property
    def enabled(self) -> bool:
        return self.mode != "OFF"

    @property
    def authoritative(self) -> bool:
        return self.mode in AUTHORITATIVE

    # ── startup ───────────────────────────────────────────────────────────
    def startup(self, *, candidate_sha: str | None = None, stage_c_result=None,
                hook_profile: str | None = None) -> None:
        """Validate the bundle, the mode and the hook profile BEFORE any
        entry. Never raises; every failure is a HALT condition."""
        self.started = True
        self.hook_profile = hook_profile
        if self.mode == "OFF":
            return
        if self.mode == "INVALID":
            self.halts.raise_halt("AI_EXECUTION_MODE_INVALID", source="AI_RUNTIME")
            return
        if self.journal is None:
            self.journal = DurableAIJournal()
        try:
            self.bundle = load_bundle(self.env)
        except Exception as exc:   # ModelIntegrityError, AIRuntimeError, JSON, OSError
            self.halts.raise_halt("MODEL_ARTIFACT_MISMATCH", source="AI_STARTUP", detail=str(exc)[:120])
            return
        state = self.bundle.manifest.get("lifecycle_state")
        if not lifecycle.mode_allowed(state, self.mode):
            self.halts.raise_halt("MODEL_ARTIFACT_MISMATCH", source="AI_STARTUP",
                                  detail=f"lifecycle {state} not allowed in {self.mode}")
        trained = self.bundle.manifest.get("hook_profile")
        if self.authoritative and (hook_profile is None or trained != hook_profile):
            self.halts.raise_halt("AI_HOOK_PROFILE_MISMATCH", source="AI_STARTUP",
                                  detail=f"bundle={trained} engine={hook_profile}")
        if self.mode == "PAPER" and not self.paper_trade:
            self.halts.raise_halt("STAGE_C_EVIDENCE_INVALID", source="AI_STARTUP",
                                  detail="AI PAPER requires PAPER_TRADE engine")
        if self.mode == "LIVE":
            self.live_blockers = authorize_ai_live(stage_c_result)
            if self.live_blockers or self.paper_trade:
                self.halts.raise_halt("STAGE_C_EVIDENCE_INVALID", source="AI_STARTUP",
                                      detail=",".join(self.live_blockers) or "LIVE on PAPER engine")
        self.authority = dec.AIDecisionAuthority(self.bundle, pinned_bundle_sha=self.env.get("AI_BUNDLE_SHA256"),
                                                 candidate_sha=candidate_sha)

    @staticmethod
    def event_ts(now_ms: int) -> int:
        """A market event = one 15m decision boundary (as in the replay)."""
        return (int(now_ms) // EVENT_MS) * EVENT_MS

    def observation_line(self, *, candidate_sha=None, deployment_id=None) -> str:
        b = self.bundle
        return ai_identity.observation_line(
            candidate_sha=candidate_sha, deployment_id=deployment_id,
            mode=self.mode if self.mode in MODES else "UNAVAILABLE", ai_version=dec.AI_VERSION,
            bundle_sha256=(b.sha256 if b else None), policy_sha256=(b.policy.sha256 if b else None),
            feature_schema_sha256=fx.schema_hash())

    def _journal_failure(self, exc, source: str) -> str:
        cond = ("AI_JOURNAL_INTEGRITY_FAILURE"
                if isinstance(exc, (JournalIntegrityError, InvalidJournalTransition))
                else "DB_RECONCILIATION_UNCERTAIN")
        self.halts.raise_halt(cond, source=source, detail=type(exc).__name__)
        return cond

    # ── restart recovery ─────────────────────────────────────────────────
    async def recover(self, lookup) -> dict:
        """Reconcile every non-final durable decision by client_oid BEFORE any
        new AI-authorized entry. ``lookup(client_oid)`` -> 'FOUND' / 'NOT_FOUND'
        or raises (unknown => HALT RECONCILIATION_UNCERTAIN). Never resubmits.
        A record failing its digest => HALT AI_JOURNAL_INTEGRITY_FAILURE."""
        out = {"open": 0, "found": 0, "not_found": 0, "uncertain": 0}
        if self.mode in ("OFF", "INVALID"):
            self.recovered = self.mode == "OFF"
            return out
        try:
            rows = await self.journal.open_decisions()
        except Exception as exc:
            self._journal_failure(exc, "AI_RECOVERY")
            return out
        out["open"] = len(rows)
        for r in rows:
            d = (r.get("record") or {}).get("decision") or {}
            self._events.add((d.get("symbol"), d.get("timestamp"), d.get("direction_proposed")))
            oid = r.get("client_oid")
            try:
                if not oid:
                    raise AIRuntimeError("open decision without client_oid")
                res = await lookup(oid)
            except Exception as exc:
                out["uncertain"] += 1
                self.halts.raise_halt("RECONCILIATION_UNCERTAIN", source="AI_RECOVERY",
                                      detail=f"{oid}:{type(exc).__name__}")
                try:
                    await self.journal.set_status(r["decision_id"], "PENDING_UNKNOWN", strict=False)
                except Exception as exc2:
                    self._journal_failure(exc2, "AI_RECOVERY")
                continue
            out["found" if res == "FOUND" else "not_found"] += 1
            try:
                await self.journal.set_status(r["decision_id"],
                                              FINAL_AFTER_RECONCILE.get(res, "RECONCILED_NOT_FOUND"),
                                              strict=True, reconciled_at_ms=self.clock_ms())
            except Exception as exc:
                out["uncertain"] += 1
                self._journal_failure(exc, "AI_RECOVERY")
        self.recovered = out["uncertain"] == 0 and not self.halts.halted
        return out

    # ── pre-trade gate ───────────────────────────────────────────────────
    async def gate(self, obs, k15, k1h, k4h, *, taker_fee: float, now_ms: int | None = None) -> GateOutcome:
        """``obs`` = bot.ai.hook.HookObservation built at the real call site."""
        from bot.ai import hook as ai_hook
        if self.mode == "OFF":
            return GateOutcome(True, False, "AI_OFF")
        auth = self.authoritative
        if not self.started:
            return GateOutcome(not auth, auth, "AI_NOT_STARTED")
        if self.halts.halted or self.authority is None:
            return GateOutcome(not auth, auth, "AI_HALTED:" + ",".join(sorted(self.halts.active)))
        if auth and not self.recovered:
            return GateOutcome(False, True, "AI_RECOVERY_NOT_COMPLETE")
        if obs.population != ai_hook.POPULATION:
            return GateOutcome(not auth, auth, "AI_HOOK_POPULATION_UNKNOWN")
        profile_ok = obs.profile == self.bundle.manifest.get("hook_profile")
        if auth and not profile_ok:
            self.halts.raise_halt("AI_HOOK_PROFILE_MISMATCH", source="AI_GATE", detail=obs.profile)
            return GateOutcome(False, True, "AI_HOOK_PROFILE_MISMATCH")
        now_ms = int(self.clock_ms() if now_ms is None else now_ms)
        try:
            fv, regime = ai_hook.features(obs, k15, k1h, k4h)
        except Exception as exc:
            return GateOutcome(not auth, auth, f"AI_FEATURES_UNAVAILABLE:{type(exc).__name__}")
        costs = runtime_costs(cost_fraction=obs.cost_fraction,
                              stop_distance_pct=(abs(obs.entry - obs.stop) / obs.entry if obs.entry > 0 else 0.0),
                              taker_fee=taker_fee)
        d = self.authority.decide(symbol=obs.symbol, direction=obs.direction, features=fv, regime=regime,
                                  now_ms=now_ms, **costs)
        event = (obs.symbol, d.timestamp, obs.direction)
        extra = {"hook_observation": obs.to_dict(), "hook_profile_matches_bundle": profile_ok}
        if auth:
            try:
                prior = await self.journal.get(d.decision_id)
            except Exception as exc:
                self._journal_failure(exc, "AI_JOURNAL")
                return GateOutcome(False, True, "AI_JOURNAL_UNAVAILABLE", d)
            if event in self._events or (prior is not None and prior[0] != "ABSTAIN"):
                return GateOutcome(False, True, "DUPLICATE_MARKET_EVENT", d)
            if prior is not None:                         # same observation already abstained
                return GateOutcome(False, True, "AI_ABSTAIN:" + ",".join(d.vetoes or ["PRIOR_ABSTAIN"]), d)
        if auth:
            status = "APPROVED" if d.is_trade else "ABSTAIN"
        else:
            status = ("SHADOW_PROFILE_MISMATCH" if not profile_ok
                      else "SHADOW_TRADE" if d.is_trade else "SHADOW_ABSTAIN")
        try:
            await self.journal.put(d, status, mode=self.mode, features=fv, extra=extra, strict=auth)
        except Exception as exc:
            if auth:     # never trade on an unjournaled decision
                return GateOutcome(False, True, f"AI_JOURNAL_WRITE_FAILED:{type(exc).__name__}", d)
        if not auth:
            return GateOutcome(True, False, status, d)
        if not d.is_trade:
            return GateOutcome(False, True, "AI_ABSTAIN:" + ",".join(d.vetoes), d)
        self._events.add(event)
        return GateOutcome(True, True, "AI_APPROVED", d)

    async def bind_intent(self, d: dec.AIDecision, client_oid: str) -> None:
        """Durably bind decision -> client_oid BEFORE dispatch (strict)."""
        try:
            await self.journal.set_status(d.decision_id, "INTENT_CREATED", strict=True, client_oid=client_oid)
        except Exception as exc:
            self._journal_failure(exc, "AI_INTENT")
            raise

    async def mark(self, d: dec.AIDecision | None, status: str, **extra) -> None:
        if d is None or not self.authoritative or self.journal is None:
            return
        try:
            await self.journal.set_status(d.decision_id, status, strict=True, **extra)
        except Exception as exc:
            self._journal_failure(exc, "AI_MARK")

    def status(self) -> dict:
        return {"mode": self.mode, "halted": self.halts.halted, "halts": sorted(self.halts.active),
                "recovered": self.recovered,
                "bundle_sha256": self.bundle.sha256 if self.bundle else None,
                "policy_sha256": self.bundle.policy.sha256 if self.bundle else None,
                "feature_schema_sha256": fx.schema_hash(), "ai_version": dec.AI_VERSION,
                "live_blockers": list(self.live_blockers),
                "generated_at": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
