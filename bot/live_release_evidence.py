"""Stage-C LIVE release evidence: schema, parser and verifier (read-only).

The envelope (``BGX_LIVE_RELEASE_EVIDENCE_V1``, see RELEASE_EVIDENCE.md) is a
CLAIM. Nothing in it is trusted because it appears in a JSON file. Every claim
is re-read from a ``TrustedSources`` provider and compared field by field:

  railway         <- control-plane deployment metadata (status, commit, IDs)
  runtime_policy  <- the LIVE_POLICY_OBSERVATION_V2 line in THAT deployment's logs
  ci / protection <- exact-SHA workflow runs from the CI provider
  human_approval  <- the approval record from the approval provider
  pinned IDs      <- reviewed production service/environment identifiers

No production provider is implemented in this repository today, so the CLI
always passes ``sources=None`` and every live verdict is BLOCK
(``LIVE_PROVENANCE_SOURCE_UNAVAILABLE``). A provider interface exposes READ
methods only; this module never deploys, restarts, sets variables or sends
orders.

Stale-evidence limits are PREDECLARED here (fixed before any Stage-C evidence
exists; never tuned to a result).
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import re
from typing import Protocol

from bot import policy_attestation as pa

SCHEMA = "BGX_LIVE_RELEASE_EVIDENCE_V1"

# ── Predeclared stale-evidence limits (seconds) ──
MAX_RUNTIME_OBSERVATION_AGE_S = 6 * 3600       # runtime line must be recent at verification
MAX_CI_EVIDENCE_AGE_S = 7 * 86400              # exact-SHA CI runs
MAX_PROTECTION_EVIDENCE_AGE_S = 7 * 86400
MAX_APPROVAL_AGE_S = 24 * 3600                 # human approval
MAX_CLOCK_SKEW_S = 300                         # future timestamps tolerated at most this much

REQUIRED_CI_WORKFLOWS: tuple[str, ...] = (
    "Quality Check", "Supply Chain Security", "Runtime Truth Candidate",
)
REQUIRED_PROTECTION_CHECKS: tuple[str, ...] = (
    "release_proof", "protection_readiness_authority", "prelive_protection_failclosed",
    "durable_execution_restart", "kucoin_native_tpsl",
)
APPROVAL_KINDS = ("GITHUB_PROTECTED_ENVIRONMENT_APPROVAL", "RELEASE_RECORD")
TRUSTED_SOURCE_KINDS = ("READ_ONLY_CONTROL_PLANE",)

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


class EvidenceError(ValueError):
    pass


class TrustedSources(Protocol):
    """Read-only evidence providers. Implementations must not mutate anything."""

    source_kind: str

    def pinned_production(self) -> dict: ...                      # {service_id, environment_id}
    def railway_deployment(self, deployment_id: str) -> dict: ...  # control-plane metadata
    def railway_deployment_logs(self, deployment_id: str) -> list: ...
    def ci_run(self, run_id: str) -> dict: ...                     # {id, name, head_sha, conclusion, completed_at}
    def approval(self, reference: str) -> dict: ...                # approval record


# Read methods a provider may expose. The verifier calls nothing else.
READ_METHODS = ("pinned_production", "railway_deployment", "railway_deployment_logs",
                "ci_run", "approval")


def _ts(value) -> _dt.datetime:
    if not isinstance(value, str):
        raise EvidenceError("timestamp missing")
    try:
        t = _dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise EvidenceError(f"bad timestamp {value!r}") from exc
    return t.replace(tzinfo=_dt.timezone.utc)


def _req(d, key, kind=str):
    if not isinstance(d, dict) or key not in d or not isinstance(d[key], kind) or d[key] in ("", None):
        raise EvidenceError(f"missing field {key}")
    return d[key]


def protection_digest(protection: dict) -> str:
    body = {k: v for k, v in protection.items() if k != "evidence_sha256"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def parse(envelope) -> dict:
    """Structural validation only. Raises EvidenceError."""
    if isinstance(envelope, (str, bytes)):
        try:
            envelope = json.loads(envelope)
        except json.JSONDecodeError as exc:
            raise EvidenceError("not JSON") from exc
    if not isinstance(envelope, dict) or envelope.get("schema") != SCHEMA:
        raise EvidenceError("schema mismatch")
    sha = _req(envelope, "candidate_sha")
    if not _SHA_RE.match(sha):
        raise EvidenceError("candidate_sha must be a full 40-hex commit")
    _ts(_req(envelope, "generated_at_utc"))
    rw = _req(envelope, "railway", dict)
    for k in ("deployment_id", "service_id", "environment_id", "deployment_commit_sha",
              "deployment_status"):
        _req(rw, k)
    rp = _req(envelope, "runtime_policy", dict)
    _req(rp, "candidate_sha")
    if not _HEX64_RE.match(_req(rp, "policy_sha256")):
        raise EvidenceError("runtime policy_sha256 malformed")
    _ts(_req(rp, "observed_at_utc"))
    ci = _req(envelope, "ci", dict)
    _req(ci, "exact_sha")
    _req(ci, "required_run_ids", dict)
    prot = _req(envelope, "protection", dict)
    for k in ("evidence_id", "candidate_sha", "result", "evidence_sha256"):
        _req(prot, k)
    _ts(_req(prot, "generated_at_utc"))
    _req(prot, "checks", dict)
    ha = _req(envelope, "human_approval", dict)
    for k in ("approval_reference", "candidate_sha", "approver_reference", "kind"):
        _req(ha, k)
    _ts(_req(ha, "approved_at_utc"))
    return envelope


def _age_ok(t: _dt.datetime, now: _dt.datetime, max_age_s: int) -> bool:
    delta = (now - t).total_seconds()
    return -MAX_CLOCK_SKEW_S <= delta <= max_age_s


def verify(envelope, *, artifact: dict | None, sources: TrustedSources | None,
           now: _dt.datetime | None = None) -> dict:
    """Verify a Stage-C envelope against trusted sources. Never mutates anything.

    Returns component verdicts and blockers. Every component defaults to
    BLOCK; only a positive, source-confirmed comparison flips it.
    """
    now = now or _dt.datetime.now(_dt.timezone.utc)
    b: list[str] = []
    comp = {"POLICY_CONTENT_MATCH": "BLOCK", "LIVE_PROVENANCE_AUTHENTICATED": "BLOCK",
            "EXACT_DEPLOYMENT_SHA_MATCH": "BLOCK", "CI_EVIDENCE": "BLOCK",
            "PROTECTION_EVIDENCE": "BLOCK", "HUMAN_APPROVAL_EVIDENCE": "BLOCK"}
    out = {"schema": SCHEMA, "components": comp, "blockers": b, "source_authenticated": False,
           "mutations_performed": 0}
    if envelope is None:
        b.append("RELEASE_EVIDENCE_MISSING")
    else:
        try:
            envelope = parse(envelope)
        except EvidenceError as exc:
            b.append("RELEASE_EVIDENCE_MALFORMED")
            out["error"] = str(exc)
            envelope = None
    if sources is None or getattr(sources, "source_kind", None) not in TRUSTED_SOURCE_KINDS:
        b.append("LIVE_PROVENANCE_SOURCE_UNAVAILABLE")
        sources = None
    art = artifact if isinstance(artifact, dict) else {}
    replay_policy_sha = ((art.get("live_policy_observation") or {}).get("replay_policy_sha"))
    if envelope is None or sources is None:
        out["blockers"] = sorted(set(b))
        return out

    cand = envelope["candidate_sha"]
    if art.get("candidate_sha") != cand:
        b.append("ARTIFACT_CANDIDATE_SHA_MISMATCH")

    # ── Railway control plane (never trusted from the envelope) ──
    rw = envelope["railway"]
    pinned = sources.pinned_production() or {}
    dep = sources.railway_deployment(rw["deployment_id"]) or {}
    rail_ok = True
    for field, key in (("deployment_id", "id"), ("service_id", "service_id"),
                       ("environment_id", "environment_id"),
                       ("deployment_commit_sha", "commit_sha"), ("deployment_status", "status")):
        if dep.get(key) != rw.get(field):
            b.append(f"RAILWAY_{field.upper()}_NOT_CONFIRMED")
            rail_ok = False
    if dep.get("status") != "SUCCESS":
        b.append("RAILWAY_DEPLOYMENT_NOT_SUCCESS")
        rail_ok = False
    if not pinned.get("service_id") or dep.get("service_id") != pinned.get("service_id"):
        b.append("WRONG_SERVICE")
        rail_ok = False
    if not pinned.get("environment_id") or dep.get("environment_id") != pinned.get("environment_id"):
        b.append("WRONG_ENVIRONMENT")
        rail_ok = False
    try:
        deployed_at = _ts(dep.get("created_at"))
    except EvidenceError:
        deployed_at = None
        b.append("RAILWAY_DEPLOYMENT_TIME_UNKNOWN")
        rail_ok = False

    # ── Runtime observation from THAT deployment's logs ──
    rp = envelope["runtime_policy"]
    obs = [o for o in pa.find_observations(sources.railway_deployment_logs(rw["deployment_id"]) or [])
           if o["identity"]["deployment_id"] == rw["deployment_id"]]
    runtime_ok = False
    rt_sha = None
    if not obs:
        b.append("RUNTIME_POLICY_OBSERVATION_NOT_FOUND_IN_DEPLOYMENT_LOGS")
    else:
        o = max(obs, key=lambda x: x["identity"]["generated_at"])
        ident = o["identity"]
        rt_sha = ident["candidate_sha"]
        runtime_ok = True
        observed = _ts(ident["generated_at"])
        if ident["generated_at"] != rp["observed_at_utc"] or o["sha256"] != rp["policy_sha256"]:
            b.append("RUNTIME_OBSERVATION_DIFFERS_FROM_ENVELOPE")
            runtime_ok = False
        if ident["service_id_hash"] != pa.id_hash(pinned.get("service_id")):
            b.append("WRONG_SERVICE")
            runtime_ok = False
        if ident["environment_id_hash"] != pa.id_hash(pinned.get("environment_id")):
            b.append("WRONG_ENVIRONMENT")
            runtime_ok = False
        if deployed_at is not None and observed < deployed_at:
            b.append("RUNTIME_OBSERVATION_PREDATES_DEPLOYMENT")
            runtime_ok = False
        if not _age_ok(observed, now, MAX_RUNTIME_OBSERVATION_AGE_S):
            b.append("RUNTIME_OBSERVATION_STALE")
            runtime_ok = False
        if replay_policy_sha and o["sha256"] == replay_policy_sha:
            comp["POLICY_CONTENT_MATCH"] = "PASS"
        else:
            b.append("POLICY_CONTENT_MISMATCH")
    if rail_ok and runtime_ok:
        comp["LIVE_PROVENANCE_AUTHENTICATED"] = "PASS"
    if (rail_ok and runtime_ok and art.get("candidate_sha") == cand == dep.get("commit_sha")
            == rt_sha == rp.get("candidate_sha")):
        comp["EXACT_DEPLOYMENT_SHA_MATCH"] = "PASS"
    else:
        b.append("EXACT_DEPLOYMENT_SHA_NOT_PROVEN")

    def run_ok(run_id, expect_name=None, max_age=MAX_CI_EVIDENCE_AGE_S) -> bool:
        r = sources.ci_run(str(run_id)) or {}
        if r.get("head_sha") != cand or r.get("conclusion") != "success":
            return False
        if expect_name is not None and r.get("name") != expect_name:
            return False
        try:
            return _age_ok(_ts(r.get("completed_at")), now, max_age)
        except EvidenceError:
            return False

    # ── CI (exact SHA, re-read from the CI provider) ──
    ci = envelope["ci"]
    ids = ci["required_run_ids"]
    ci_ok = ci["exact_sha"] == cand
    for name in REQUIRED_CI_WORKFLOWS:
        if name not in ids or not run_ok(ids[name], name):
            b.append(f"CI_NOT_PROVEN_{name.upper().replace(' ', '_')}")
            ci_ok = False
    if ci_ok:
        comp["CI_EVIDENCE"] = "PASS"

    # ── Protection readiness: structured, digest-bound, exact-SHA runs ──
    prot = envelope["protection"]
    prot_ok = (prot["candidate_sha"] == cand and prot["result"] == "PASS"
               and prot["evidence_sha256"] == protection_digest(prot)
               and _age_ok(_ts(prot["generated_at_utc"]), now, MAX_PROTECTION_EVIDENCE_AGE_S))
    for name in REQUIRED_PROTECTION_CHECKS:
        chk = prot["checks"].get(name)
        if (not isinstance(chk, dict) or chk.get("result") != "PASS"
                or not chk.get("run_id") or not run_ok(chk["run_id"])):
            b.append(f"PROTECTION_CHECK_NOT_PROVEN_{name.upper()}")
            prot_ok = False
    if prot_ok:
        comp["PROTECTION_EVIDENCE"] = "PASS"
    else:
        b.append("PROTECTION_EVIDENCE_NOT_PROVEN")

    # ── Human approval: structured record from the approval provider ──
    ha = envelope["human_approval"]
    rec = sources.approval(ha["approval_reference"]) or {}
    ha_ok = (ha["kind"] in APPROVAL_KINDS and ha["candidate_sha"] == cand
             and rec.get("state") == "APPROVED"
             and all(rec.get(k) == ha.get(k) for k in
                     ("approval_reference", "candidate_sha", "approver_reference", "kind",
                      "approved_at_utc"))
             and _age_ok(_ts(ha["approved_at_utc"]), now, MAX_APPROVAL_AGE_S))
    if ha_ok:
        comp["HUMAN_APPROVAL_EVIDENCE"] = "PASS"
    else:
        b.append("HUMAN_APPROVAL_NOT_PROVEN")

    out["source_authenticated"] = comp["LIVE_PROVENANCE_AUTHENTICATED"] == "PASS"
    out["blockers"] = sorted(set(b))
    return out
