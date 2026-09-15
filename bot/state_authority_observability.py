"""Non-secret persistence-authority observability for production.

Emits a stable one-way fingerprint of the configured PostgreSQL endpoint so
operators can prove that stateful subsystems share one persistence authority
without logging DATABASE_URL, credentials, hostname, username, or database name.
This module is observability-only and never changes database or trading state.
"""
from __future__ import annotations

import hashlib
import os
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


def log_database_authority(log) -> None:
    log.info(
        "[STATE_AUTHORITY] database=postgres fingerprint=%s "
        "credential_material=false execution_effect=NONE",
        database_authority_fingerprint(),
    )
