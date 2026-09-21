"""Non-secret persistence-authority observability for production.

Emits a stable one-way fingerprint of the configured PostgreSQL endpoint so
operators can prove that stateful subsystems share one persistence authority
without logging DATABASE_URL, credentials, hostname, username, or database name.
This module is observability-only and never changes database or trading state.
"""
from __future__ import annotations

import hashlib
import os
import re
from urllib.parse import urlsplit


def database_authority_fingerprint() -> str:
    raw = os.environ.get("DATABASE_URL", "").strip()
    if not raw:
        return "UNCONFIGURED"
    try:
        parsed = urlsplit(raw)
        canonical = "|".join((
            (parsed.scheme or "").lower(),
            (parsed.hostname or "").lower(),
            str(parsed.port or ""),
            parsed.path or "",
        ))
        if not parsed.hostname:
            return "MALFORMED"
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    except Exception:
        return "MALFORMED"


def database_authority_id() -> str:
    """Return an explicitly configured non-secret operational DB identifier."""
    raw = os.environ.get("DB_AUTHORITY_ID", "").strip()
    if not raw:
        return "UNSET"
    # Only service-name / UUID style identifiers are accepted. URLs, DSNs,
    # credentials and query strings are deliberately rejected.
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", raw):
        return "INVALID_NONSECRET_ID"
    return raw


def log_database_authority(log) -> None:
    authority_id = database_authority_id()
    log.info(
        "[STATE_AUTHORITY] database=postgres authority_id=%s fingerprint=%s "
        "credential_material=false execution_effect=NONE",
        authority_id,
        database_authority_fingerprint(),
    )
