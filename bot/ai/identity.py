"""AI identity observation and Stage-C AI identity verification (read-only).

At startup, when AI_EXECUTION_MODE is not OFF, the bot logs ONE sanitized line

    [AI_IDENTITY_OBSERVATION_V1] format=1 candidate_sha=<git sha> deployment_id=<id>
        generated_at=<UTC> ai_mode=<SHADOW|PAPER|LIVE|UNAVAILABLE> ai_version=<...>
        bundle_sha256=<hex|UNAVAILABLE> policy_sha256=<hex|UNAVAILABLE>
        feature_schema_sha256=<hex> identity_sha256=<hex>

Only identifiers/hashes: no model weights, thresholds, secrets or URLs.
Like LIVE_POLICY_OBSERVATION_V2, the line proves payload INTEGRITY only; source
provenance comes from the Stage-C verifier reading THAT deployment's logs from
a trusted, read-only control-plane provider.

AI LIVE requires ``verify_ai_identity`` == PASS: the envelope's ``ai_identity``
claim, the runtime observation in the exact deployment's logs, and the
APPROVED identity served by the protected release environment
(``approved_ai_identity``, lifecycle LIVE_CHAMPION) must agree on code SHA,
bundle sha256, decision-policy sha256, feature-schema sha256 and AI version.
Missing, unknown, changed or unapproved identity => BLOCK.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import re

TAG = "[AI_IDENTITY_OBSERVATION_V1]"
FIELDS = ("format", "candidate_sha", "deployment_id", "generated_at", "ai_mode", "ai_version",
          "bundle_sha256", "policy_sha256", "feature_schema_sha256", "identity_sha256")
IDENTITY_KEYS = ("candidate_sha", "ai_version", "bundle_sha256", "policy_sha256", "feature_schema_sha256")
_HEX64 = r"[0-9a-f]{64}"
_RE = {
    "format": re.compile(r"^1$"),
    "candidate_sha": re.compile(r"^([0-9a-f]{40}|UNAVAILABLE)$"),
    "deployment_id": re.compile(r"^([A-Za-z0-9-]{1,64}|UNAVAILABLE)$"),
    "generated_at": re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"),
    "ai_mode": re.compile(r"^(SHADOW|PAPER|LIVE|OFF|UNAVAILABLE)$"),
    "ai_version": re.compile(r"^[A-Z0-9_]{1,64}$"),
    "bundle_sha256": re.compile(rf"^({_HEX64}|UNAVAILABLE)$"),
    "policy_sha256": re.compile(rf"^({_HEX64}|UNAVAILABLE)$"),
    "feature_schema_sha256": re.compile(rf"^{_HEX64}$"),
    "identity_sha256": re.compile(rf"^{_HEX64}$"),
}
REQUIRED_LIFECYCLE = "LIVE_CHAMPION"
MAX_OBSERVATION_AGE_S = 6 * 3600
MAX_CLOCK_SKEW_S = 300
COMPONENT = "AI_IDENTITY_MATCH"


def identity_digest(ident: dict) -> str:
    body = "|".join(f"{k}={ident.get(k)}" for k in IDENTITY_KEYS)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def observation_line(*, candidate_sha, deployment_id, mode, ai_version, bundle_sha256, policy_sha256,
                     feature_schema_sha256, now=None) -> str:
    ident = {"format": "1", "candidate_sha": candidate_sha or "UNAVAILABLE",
             "deployment_id": deployment_id or "UNAVAILABLE",
             "generated_at": (now or _dt.datetime.now(_dt.timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ"),
             "ai_mode": mode or "UNAVAILABLE", "ai_version": ai_version,
             "bundle_sha256": bundle_sha256 or "UNAVAILABLE",
             "policy_sha256": policy_sha256 or "UNAVAILABLE",
             "feature_schema_sha256": feature_schema_sha256}
    ident["identity_sha256"] = identity_digest(ident)
    return TAG + " " + " ".join(f"{k}={ident[k]}" for k in FIELDS)


def parse(line: str) -> dict:
    text = str(line).strip()
    idx = text.find(TAG)
    if idx < 0:
        raise ValueError("tag not found")
    out = {}
    for tok in text[idx + len(TAG):].split():
        if "=" not in tok:
            raise ValueError("malformed token")
        k, v = tok.split("=", 1)
        if k not in _RE or k in out or not _RE[k].match(v):
            raise ValueError(f"bad field {k}")
        out[k] = v
    if tuple(out) != FIELDS:
        raise ValueError("fields missing or out of order")
    if identity_digest(out) != out["identity_sha256"]:
        raise ValueError("identity_sha256 does not match")
    return out


def find_observations(lines) -> list[dict]:
    res = []
    for line in lines or ():
        if TAG in str(line):
            try:
                res.append(parse(line))
            except ValueError:
                continue
    return res


def _ts(v):
    try:
        return _dt.datetime.strptime(str(v), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_dt.timezone.utc)
    except ValueError:
        return None


def verify_ai_identity(envelope: dict | None, *, sources, now=None) -> dict:
    """Stage-C AI identity binding. Read-only. Every path defaults to BLOCK."""
    now = now or _dt.datetime.now(_dt.timezone.utc)
    b: list[str] = []
    out = {"component": COMPONENT, "verdict": "BLOCK", "blockers": b, "approved_identity": None}
    claim = (envelope or {}).get("ai_identity") if isinstance(envelope, dict) else None
    cand = (envelope or {}).get("candidate_sha") if isinstance(envelope, dict) else None
    dep_id = ((envelope or {}).get("railway") or {}).get("deployment_id") if isinstance(envelope, dict) else None
    if sources is None or not hasattr(sources, "approved_ai_identity"):
        b.append("AI_IDENTITY_MISSING" if not isinstance(claim, dict) else "AI_IDENTITY_SOURCE_UNAVAILABLE")
        out["blockers"] = sorted(set(b))
        return out
    approved = sources.approved_ai_identity(str(cand)) or {}
    out["approved_identity"] = {k: approved.get(k) for k in (*IDENTITY_KEYS, "lifecycle_state")}
    if not isinstance(claim, dict):
        b.append("AI_IDENTITY_MISSING")
        out["blockers"] = sorted(set(b))
        return out
    for k in IDENTITY_KEYS[1:]:
        if not isinstance(claim.get(k), str) or not claim.get(k) or claim.get(k) == "UNAVAILABLE":
            b.append(f"AI_IDENTITY_UNKNOWN_{k.upper()}")
    if approved.get("lifecycle_state") != REQUIRED_LIFECYCLE:
        b.append("AI_IDENTITY_NOT_APPROVED_FOR_LIVE")
    if approved.get("candidate_sha") != cand:
        b.append("AI_IDENTITY_APPROVED_FOR_OTHER_CODE")
    for k in IDENTITY_KEYS[1:]:
        if approved.get(k) != claim.get(k):
            b.append(f"AI_IDENTITY_CLAIM_DIFFERS_FROM_APPROVED_{k.upper()}")
    obs = [o for o in find_observations(sources.railway_deployment_logs(dep_id) or [])
           if o["deployment_id"] == dep_id]
    if not obs:
        b.append("AI_IDENTITY_OBSERVATION_NOT_FOUND_IN_DEPLOYMENT_LOGS")
    else:
        o = max(obs, key=lambda x: x["generated_at"])
        if o["ai_mode"] != "LIVE":
            b.append("AI_RUNTIME_MODE_NOT_LIVE")
        if o["candidate_sha"] != cand:
            b.append("AI_RUNTIME_CODE_SHA_MISMATCH")
        for k in IDENTITY_KEYS[1:]:
            if o.get(k) != approved.get(k):
                b.append(f"AI_RUNTIME_{k.upper()}_MISMATCH")
        t = _ts(o["generated_at"])
        if t is None or not (-MAX_CLOCK_SKEW_S <= (now - t).total_seconds() <= MAX_OBSERVATION_AGE_S):
            b.append("AI_IDENTITY_OBSERVATION_STALE")
    out["blockers"] = sorted(set(b))
    out["verdict"] = "PASS" if not b else "BLOCK"
    return out
