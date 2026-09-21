"""Non-secret persistence-authority observability for production.

Emits an explicit operator-assigned authority ID plus a stable one-way
fingerprint of the configured PostgreSQL endpoint.  Credentials, usernames,
hostnames and connection strings are never logged.  This module is
observability-only and never changes database or trading state.
"""
from __future__ import annotations

import hashlib
import os
import re
from urllib.parse import unquote, urlsplit


_SAFE_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}\Z")


def _sanitized_label(value, *, missing: str) -> str:
    text = str(value or "").strip()
    if not text:
        return missing
    if not _SAFE_LABEL.fullmatch(text):
        return "INVALID"
    return text


def database_authority_id() -> str:
    """Return a log-safe, non-secret stable service identifier."""
    return _sanitized_label(
        os.environ.get("DB_AUTHORITY_ID", ""), missing="UNCONFIGURED"
    )


def configured_database_identity() -> tuple[str, str]:
    """Return only the configured backend and sanitized database name."""
    raw = os.environ.get("DATABASE_URL", "").strip()
    if not raw:
        return "unconfigured", "UNCONFIGURED"
    try:
        parsed = urlsplit(raw)
        scheme = (parsed.scheme or "").lower()
        backend = "postgres" if scheme in {"postgres", "postgresql"} else "other"
        database = unquote((parsed.path or "").lstrip("/").split("/", 1)[0])
        return backend, _sanitized_label(database, missing="UNCONFIGURED")
    except Exception:
        return "malformed", "INVALID"


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


def log_database_authority(
    log,
    *,
    backend: str | None = None,
    database: str | None = None,
    schema: str | None = None,
    event: str = "configured",
) -> None:
    configured_backend, configured_database = configured_database_identity()
    safe_backend = _sanitized_label(
        backend or configured_backend, missing="UNCONFIGURED"
    )
    safe_database = _sanitized_label(
        database or configured_database, missing="UNCONFIGURED"
    )
    safe_schema = _sanitized_label(
        schema, missing="CONNECTION_PENDING"
    )
    safe_event = _sanitized_label(event, missing="UNKNOWN")
    log.info(
        "[STATE_AUTHORITY] event=%s backend=%s database=%s schema=%s "
        "authority_id=%s fingerprint=%s sanitized=true "
        "credential_material=false execution_effect=NONE",
        safe_event,
        safe_backend,
        safe_database,
        safe_schema,
        database_authority_id(),
        database_authority_fingerprint(),
    )
