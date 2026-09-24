"""Stage-C LIVE release evidence: schema, parser and verifier (read-only).

The envelope (``BGX_LIVE_RELEASE_EVIDENCE_V1``, see RELEASE_EVIDENCE.md) is a
CLAIM. Nothing in it is trusted because it appears in a JSON file. Every claim
is re-read from a ``TrustedSources`` provider and compared field by field:

  research_artifact <- the exact-SHA successful "NEXUS Real OOS Replay" run and
                       ITS uploaded nexus_oos_real_replay.json (bytes + digest).
                       The research gate is evaluated on THAT artifact only.
  railway           <- control-plane deployment metadata (status, commit, IDs)
  runtime_policy    <- the LIVE_POLICY_OBSERVATION_V2 line in THAT deployment's logs
  ci                <- exact-SHA workflow runs
  protection        <- per-check job identity, or a PROTECTION_EVIDENCE_V1 artifact
  human_approval    <- the approval record, bound to this release instance
  release_verifier  <- identity of the verifier pinned by the protected
                       release environment (never the candidate's own code)

TRUST BOUNDARY. This module is a reference implementation that lives in the
candidate repository. It is NOT the Stage-C root of trust: the authoritative
verifier runs from a protected, pinned location (RELEASE_EVIDENCE.md §7), and
provider trust comes from that protected environment and its credentials, not
from the ``source_kind`` field (which any object can claim). No production
provider is implemented here, so the CLI passes ``sources=None`` and every live
verdict is BLOCK (``LIVE_PROVENANCE_SOURCE_UNAVAILABLE``).

Read-only: providers expose READ methods only; this module never deploys,
restarts, sets variables or sends orders. Stale-evidence limits are
PREDECLARED below (fixed before any Stage-C evidence exists).
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import re
from typing import Protocol

from bot import policy_attestation as pa

SCHEMA = "BGX_LIVE_RELEASE_EVIDENCE_V1"
RELEASE_VERIFIER_VERSION = "BGX_RELEASE_VERIFIER_V2"

# ── Predeclared stale-evidence limits (seconds) ──
MAX_RUNTIME_OBSERVATION_AGE_S = 6 * 3600       # runtime line must be recent at verification
MAX_CI_EVIDENCE_AGE_S = 7 * 86400              # exact-SHA CI runs, including the OOS run
MAX_PROTECTION_EVIDENCE_AGE_S = 7 * 86400
MAX_APPROVAL_AGE_S = 24 * 3600                 # human approval
MAX_CLOCK_SKEW_S = 300                         # future timestamps tolerated at most this much

OOS_WORKFLOW = "NEXUS Real OOS Replay"
OOS_ARTIFACT_NAME = "nexus-oos-real-replay"
REQUIRED_CI_WORKFLOWS: tuple[str, ...] = (
    "Quality Check", "Supply Chain Security", "Runtime Truth Candidate", OOS_WORKFLOW,
)
REQUIRED_PROTECTION_CHECKS: tuple[str, ...] = (
    "release_proof", "protection_readiness_authority", "prelive_protection_failclosed",
    "durable_execution_restart", "kucoin_native_tpsl",
)
PROTECTION_WORKFLOW = "BGX Protection Evidence"
PROTECTION_ARTIFACT_NAME = "protection-evidence"
PROTECTION_SCHEMA = "PROTECTION_EVIDENCE_V1"
APPROVAL_KINDS = ("GITHUB_PROTECTED_ENVIRONMENT_APPROVAL", "RELEASE_RECORD")
TRUSTED_SOURCE_KINDS = ("READ_ONLY_CONTROL_PLANE",)

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")

COMPONENTS = ("RESEARCH_ARTIFACT_AUTHENTICATED", "POLICY_CONTENT_MATCH",
              "LIVE_PROVENANCE_AUTHENTICATED", "EXACT_DEPLOYMENT_SHA_MATCH", "CI_EVIDENCE",
              "PROTECTION_EVIDENCE", "HUMAN_APPROVAL_EVIDENCE", "RELEASE_VERIFIER_TRUSTED")


class EvidenceError(ValueError):
    pass


class TrustedSources(Protocol):
    """Read-only evidence providers. Implementations must not mutate anything.

    ``source_kind`` is an interface marker only; trust comes from the protected
    execution environment that constructs the provider and holds its
    credentials. Candidate-controlled code must never inject it.
    """

    source_kind: str

    def pinned_production(self) -> dict: ...                          # {service_id, environment_id}
    def release_verifier(self) -> dict: ...                           # {version, sha, pinned_sha, source_ref}
    def railway_deployment(self, deployment_id: str) -> dict: ...
    def railway_deployment_logs(self, deployment_id: str) -> list: ...
    def ci_run(self, run_id: str) -> dict: ...                         # {id, name, head_sha, conclusion, completed_at}
    def ci_job(self, job_id: str) -> dict: ...                         # {id, run_id, name, head_sha, conclusion}
    def ci_artifact(self, run_id: str, name: str) -> dict: ...         # {name, sha256, content: bytes}
    def approval(self, reference: str) -> dict: ...
    # AI LIVE only: identity approved by the protected release environment
    # {candidate_sha, ai_version, bundle_sha256, policy_sha256,
    #  feature_schema_sha256, lifecycle_state}
    def approved_ai_identity(self, candidate_sha: str) -> dict: ...


# Read methods a provider may expose. The verifier calls nothing else.
READ_METHODS = ("pinned_production", "release_verifier", "railway_deployment",
                "railway_deployment_logs", "ci_run", "ci_job", "ci_artifact", "approval",
                "approved_ai_identity")


def _ts(value) -> _dt.datetime:
    if not isinstance(value, str):
        raise EvidenceError("timestamp missing")
    try:
        t = _dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise EvidenceError(f"bad timestamp {value!r}") from exc
    return t.replace(tzinfo=_dt.timezone.utc)


def _try_ts(value):
    try:
        return _ts(value)
    except EvidenceError:
        return None


def _req(d, key, kind=str):
    if not isinstance(d, dict) or key not in d or not isinstance(d[key], kind) or d[key] in ("", None):
        raise EvidenceError(f"missing field {key}")
    return d[key]


def _canon(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


def protection_digest(protection: dict) -> str:
    return hashlib.sha256(_canon({k: v for k, v in protection.items() if k != "evidence_sha256"})).hexdigest()


def release_evidence_digest(envelope: dict) -> str:
    """Digest of the whole envelope except the approval that binds it."""
    return hashlib.sha256(_canon({k: v for k, v in envelope.items() if k != "human_approval"})).hexdigest()


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
    ra = _req(envelope, "research_artifact", dict)
    for k in ("oos_run_id", "workflow_name", "artifact_name", "candidate_sha"):
        _req(ra, k)
    if not _HEX64_RE.match(_req(ra, "artifact_sha256")):
        raise EvidenceError("artifact_sha256 malformed")
    _ts(_req(ra, "completed_at_utc"))
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
    for k in ("evidence_id", "candidate_sha", "result", "evidence_sha256", "mode"):
        _req(prot, k)
    _ts(_req(prot, "generated_at_utc"))
    if prot["mode"] == "PER_JOB":
        _req(prot, "checks", dict)
    elif prot["mode"] == "CERTIFICATE":
        cert = _req(prot, "certificate", dict)
        for k in ("run_id", "artifact_name", "artifact_sha256"):
            _req(cert, k)
    else:
        raise EvidenceError("protection mode must be PER_JOB or CERTIFICATE")
    ha = _req(envelope, "human_approval", dict)
    for k in ("approval_reference", "candidate_sha", "approver_reference", "kind",
              "deployment_id", "research_artifact_sha256", "release_evidence_digest"):
        _req(ha, k)
    _ts(_req(ha, "approved_at_utc"))
    return envelope


def _age_ok(t, now: _dt.datetime, max_age_s: int) -> bool:
    if t is None:
        return False
    delta = (now - t).total_seconds()
    return -MAX_CLOCK_SKEW_S <= delta <= max_age_s


def _artifact_bytes(fetched) -> bytes | None:
    content = (fetched or {}).get("content")
    if isinstance(content, str):
        content = content.encode("utf-8")
    return content if isinstance(content, (bytes, bytearray)) else None


RELEASE_KINDS = ("NEXUS_ONLY", "AI_LIVE")


def verify(envelope, *, local_artifact: dict | None = None, sources: TrustedSources | None,
           now: _dt.datetime | None = None, release_authority_kind: str = "NEXUS_ONLY") -> dict:
    """Verify a Stage-C envelope against trusted sources. Never mutates anything.

    Returns component verdicts, blockers and ``trusted_artifact`` (the research
    artifact fetched from the trusted CI provider, or None). Every component
    defaults to BLOCK; only a positive, source-confirmed comparison flips it.
    A caller-supplied ``local_artifact`` is used for comparison only.
    """
    now = now or _dt.datetime.now(_dt.timezone.utc)
    b: list[str] = []
    comp = {k: "BLOCK" for k in COMPONENTS}
    out = {"schema": SCHEMA, "components": comp, "blockers": b, "source_authenticated": False,
           "trusted_artifact": None, "release_verifier_version": None,
           "release_verifier_sha": None, "mutations_performed": 0}
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
    if envelope is None or sources is None:
        out["blockers"] = sorted(set(b))
        return out
    cand = envelope["candidate_sha"]

    # ── Release verifier identity (from the protected environment) ──
    rv = sources.release_verifier() or {}
    out["release_verifier_version"] = rv.get("version")
    out["release_verifier_sha"] = rv.get("sha")
    if (rv.get("sha") and _SHA_RE.match(str(rv.get("sha"))) and rv.get("sha") == rv.get("pinned_sha")
            and rv.get("sha") != cand and rv.get("version")):
        comp["RELEASE_VERIFIER_TRUSTED"] = "PASS"
    else:
        b.append("RELEASE_VERIFIER_NOT_TRUSTED")

    def run_ok(run_id, expect_name=None, max_age=MAX_CI_EVIDENCE_AGE_S) -> dict | None:
        r = sources.ci_run(str(run_id)) or {}
        if r.get("head_sha") != cand or r.get("conclusion") != "success":
            return None
        if expect_name is not None and r.get("name") != expect_name:
            return None
        return r if _age_ok(_try_ts(r.get("completed_at")), now, max_age) else None

    # ── Research artifact: fetched from the trusted CI provider ──
    ra = envelope["research_artifact"]
    trusted = None
    oos_run = run_ok(ra["oos_run_id"], OOS_WORKFLOW)
    if (ra["workflow_name"] != OOS_WORKFLOW or ra["artifact_name"] != OOS_ARTIFACT_NAME
            or ra["candidate_sha"] != cand):
        b.append("RESEARCH_ARTIFACT_CLAIM_INVALID")
    elif oos_run is None:
        # Includes a run whose replay step succeeded but whose strict research
        # gate failed: the gate is the last step, so run.conclusion != success.
        b.append("OOS_RUN_NOT_SUCCESSFUL_ON_CANDIDATE_SHA")
    elif oos_run.get("completed_at") != ra["completed_at_utc"]:
        b.append("RESEARCH_ARTIFACT_CLAIM_INVALID")
    else:
        fetched = sources.ci_artifact(str(ra["oos_run_id"]), OOS_ARTIFACT_NAME) or {}
        content = _artifact_bytes(fetched)
        digest = hashlib.sha256(bytes(content)).hexdigest() if content is not None else None
        if (content is None or fetched.get("name") != OOS_ARTIFACT_NAME
                or digest != fetched.get("sha256") or digest != ra["artifact_sha256"]):
            b.append("RESEARCH_ARTIFACT_DIGEST_MISMATCH")
        else:
            try:
                trusted = json.loads(bytes(content).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                trusted = None
                b.append("RESEARCH_ARTIFACT_CORRUPT")
            if isinstance(trusted, dict) and trusted.get("candidate_sha") == cand:
                comp["RESEARCH_ARTIFACT_AUTHENTICATED"] = "PASS"
                out["trusted_artifact"] = trusted
            elif trusted is not None:
                b.append("RESEARCH_ARTIFACT_CANDIDATE_SHA_MISMATCH")
                trusted = None
    if isinstance(local_artifact, dict) and trusted is not None and _canon(local_artifact) != _canon(trusted):
        b.append("LOCAL_ARTIFACT_DIFFERS_FROM_TRUSTED")

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
    deployed_at = _try_ts(dep.get("created_at"))
    if deployed_at is None:
        b.append("RAILWAY_DEPLOYMENT_TIME_UNKNOWN")
        rail_ok = False

    # ── Runtime observation from THAT deployment's logs ──
    rp = envelope["runtime_policy"]
    obs = [o for o in pa.find_observations(sources.railway_deployment_logs(rw["deployment_id"]) or [])
           if o["identity"]["deployment_id"] == rw["deployment_id"]]
    runtime_ok = False
    rt_sha = None
    observed = None
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
        replay_policy_sha = ((trusted or {}).get("live_policy_observation") or {}).get("replay_policy_sha")
        if replay_policy_sha and o["sha256"] == replay_policy_sha:
            comp["POLICY_CONTENT_MATCH"] = "PASS"
        else:
            b.append("POLICY_CONTENT_MISMATCH")
    if rail_ok and runtime_ok:
        comp["LIVE_PROVENANCE_AUTHENTICATED"] = "PASS"
    if (rail_ok and runtime_ok and trusted is not None
            and trusted.get("candidate_sha") == cand == dep.get("commit_sha") == rt_sha
            == rp.get("candidate_sha")):
        comp["EXACT_DEPLOYMENT_SHA_MATCH"] = "PASS"
    else:
        b.append("EXACT_DEPLOYMENT_SHA_NOT_PROVEN")

    # ── CI (exact SHA, re-read from the CI provider) ──
    ci = envelope["ci"]
    ids = ci["required_run_ids"]
    ci_ok = ci["exact_sha"] == cand
    ci_times = []
    for name in REQUIRED_CI_WORKFLOWS:
        r = run_ok(ids[name], name) if name in ids else None
        if r is None:
            b.append(f"CI_NOT_PROVEN_{name.upper().replace(' ', '_')}")
            ci_ok = False
        else:
            ci_times.append(_try_ts(r.get("completed_at")))
    if str(ids.get(OOS_WORKFLOW)) != str(ra["oos_run_id"]):
        b.append("CI_OOS_RUN_DIFFERS_FROM_RESEARCH_ARTIFACT")
        ci_ok = False
    if ci_ok:
        comp["CI_EVIDENCE"] = "PASS"

    # ── Protection: provider-authenticated check identity ──
    prot = envelope["protection"]
    prot_times = [_try_ts(prot["generated_at_utc"])]
    prot_ok = (prot["candidate_sha"] == cand and prot["result"] == "PASS"
               and prot["evidence_sha256"] == protection_digest(prot)
               and _age_ok(_try_ts(prot["generated_at_utc"]), now, MAX_PROTECTION_EVIDENCE_AGE_S))
    if prot["mode"] == "PER_JOB":
        used_jobs = set()
        for name in REQUIRED_PROTECTION_CHECKS:
            chk = prot["checks"].get(name)
            ok = isinstance(chk, dict) and chk.get("result") == "PASS" and chk.get("job_id") and chk.get("run_id")
            if ok:
                job = sources.ci_job(str(chk["job_id"])) or {}
                prun = run_ok(chk["run_id"], max_age=MAX_PROTECTION_EVIDENCE_AGE_S)
                ok = (str(chk["job_id"]) not in used_jobs
                      and str(job.get("run_id")) == str(chk["run_id"])
                      and job.get("name") == name and job.get("head_sha") == cand
                      and job.get("conclusion") == "success" and prun is not None)
                used_jobs.add(str(chk["job_id"]))
                if prun is not None:
                    prot_times.append(_try_ts(prun.get("completed_at")))
            if not ok:
                b.append(f"PROTECTION_CHECK_NOT_PROVEN_{name.upper()}")
                prot_ok = False
    else:
        cert = prot["certificate"]
        crun = run_ok(cert["run_id"], PROTECTION_WORKFLOW, MAX_PROTECTION_EVIDENCE_AGE_S)
        cert_ok = cert["artifact_name"] == PROTECTION_ARTIFACT_NAME and crun is not None
        if crun is not None:
            prot_times.append(_try_ts(crun.get("completed_at")))
        body = None
        if cert_ok:
            fetched = sources.ci_artifact(str(cert["run_id"]), PROTECTION_ARTIFACT_NAME) or {}
            content = _artifact_bytes(fetched)
            digest = hashlib.sha256(bytes(content)).hexdigest() if content is not None else None
            cert_ok = (content is not None and digest == fetched.get("sha256") == cert["artifact_sha256"])
            if cert_ok:
                try:
                    body = json.loads(bytes(content).decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    body = None
        checks = (body or {}).get("checks") if isinstance(body, dict) else None
        cert_ok = (cert_ok and isinstance(body, dict) and body.get("schema") == PROTECTION_SCHEMA
                   and body.get("candidate_sha") == cand and isinstance(checks, dict))
        for name in REQUIRED_PROTECTION_CHECKS:
            if not cert_ok or checks.get(name) != "PASS":
                b.append(f"PROTECTION_CHECK_NOT_PROVEN_{name.upper()}")
                prot_ok = False
    if prot_ok:
        comp["PROTECTION_EVIDENCE"] = "PASS"
    else:
        b.append("PROTECTION_EVIDENCE_NOT_PROVEN")

    # ── AI identity: mandatory for AI_LIVE; reported for NEXUS_ONLY ──
    from bot.ai import identity as ai_identity
    out["release_authority_kind"] = release_authority_kind
    out["ai_identity"] = ai_identity.verify_ai_identity(envelope, sources=sources, now=now)
    ai_observed = (_try_ts(out["ai_identity"].get("observed_at"))
                   if release_authority_kind == "AI_LIVE" else None)
    if release_authority_kind not in RELEASE_KINDS:
        b.append("RELEASE_AUTHORITY_KIND_UNKNOWN")
    claimed_kind = envelope.get("release_authority_kind")
    if (claimed_kind is not None and claimed_kind != release_authority_kind) or (
            release_authority_kind == "NEXUS_ONLY" and "ai_identity" in envelope):
        b.append("RELEASE_AUTHORITY_KIND_AMBIGUOUS")
    if release_authority_kind == "AI_LIVE" and out["ai_identity"]["verdict"] != "PASS":
        b.append("AI_IDENTITY_NOT_PROVEN")

    # ── Human approval: bound to THIS release instance ──
    ha = envelope["human_approval"]
    rec = sources.approval(ha["approval_reference"]) or {}
    approved_at = _try_ts(ha["approved_at_utc"])
    # Approval is the FINAL release authorization: it must not predate any
    # automated Stage-C evidence it authorizes, including protection evidence.
    prelive_times = [t for t in [deployed_at, observed, ai_observed,
                                 _try_ts((oos_run or {}).get("completed_at")),
                                 *ci_times, *prot_times] if t is not None]
    prelive_done = max(prelive_times) if prelive_times else None
    covered = rec.get("deployment_id") == rw["deployment_id"] or (
        rw["deployment_id"] in (rec.get("covered_deployment_ids") or []))
    ha_ok = (ha["kind"] in APPROVAL_KINDS and ha["candidate_sha"] == cand
             and ha["deployment_id"] == rw["deployment_id"] and covered
             and ha["research_artifact_sha256"] == ra["artifact_sha256"]
             and ha["release_evidence_digest"] == release_evidence_digest(envelope)
             and rec.get("state") == "APPROVED"
             and all(rec.get(k) == ha.get(k) for k in
                     ("approval_reference", "candidate_sha", "approver_reference", "kind",
                      "approved_at_utc", "research_artifact_sha256", "release_evidence_digest"))
             and prelive_done is not None and approved_at is not None and approved_at >= prelive_done
             and _age_ok(approved_at, now, MAX_APPROVAL_AGE_S))
    if ha_ok:
        comp["HUMAN_APPROVAL_EVIDENCE"] = "PASS"
    else:
        b.append("HUMAN_APPROVAL_NOT_PROVEN")

    out["source_authenticated"] = (comp["LIVE_PROVENANCE_AUTHENTICATED"] == "PASS"
                                   and comp["RESEARCH_ARTIFACT_AUTHENTICATED"] == "PASS"
                                   and comp["RELEASE_VERIFIER_TRUSTED"] == "PASS")
    out["blockers"] = sorted(set(b))
    return out
