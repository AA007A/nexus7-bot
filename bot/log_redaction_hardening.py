"""Runtime log redaction for credentials and authentication material.

This module changes only log rendering. It does not alter request signing,
credentials, trading state, sizing, PAPER/LIVE selection, or exchange I/O.
"""
from __future__ import annotations

import logging
import os
import re

_SECRET_ENV_NAMES = (
    "KUCOIN_API_KEY",
    "KUCOIN_API_SECRET",
    "KUCOIN_API_PASSPHRASE",
    "TELEGRAM_TOKEN",
    "BOT_API_SECRET",
    "DATABASE_URL",
)

_PATTERNS = (
    (re.compile(r"(KuCoin API Key:\s*)\S+", re.IGNORECASE), r"\1[REDACTED]"),
    (re.compile(r"(sign\[:12\]=)\S+", re.IGNORECASE), r"\1[REDACTED]"),
    (re.compile(r"(KC-API-(?:KEY|SIGN|PASSPHRASE)\s*[:=]\s*)\S+", re.IGNORECASE), r"\1[REDACTED]"),
    (re.compile(r"(Passphrase contém chars especiais:\s*)\[[^\]]*\]", re.IGNORECASE), r"\1[REDACTED]"),
)


def _secret_values():
    values = []
    for name in _SECRET_ENV_NAMES:
        value = os.environ.get(name, "")
        if len(value) >= 4:
            values.append(value)
    return sorted(set(values), key=len, reverse=True)


def redact_text(value):
    """Return a log-safe string while preserving non-secret diagnostics."""
    if not isinstance(value, str):
        return value
    text = value
    for secret in _secret_values():
        text = text.replace(secret, "[REDACTED]")
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _redact_arg(value):
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, tuple):
        return tuple(_redact_arg(item) for item in value)
    if isinstance(value, list):
        return [_redact_arg(item) for item in value]
    if isinstance(value, dict):
        return {key: _redact_arg(item) for key, item in value.items()}
    return value


def install():
    """Install an idempotent global LogRecord factory redaction layer."""
    previous = logging.getLogRecordFactory()
    if getattr(previous, "_nexus_secret_redaction", False):
        return False

    def factory(*args, **kwargs):
        record = previous(*args, **kwargs)
        record.msg = redact_text(record.msg)
        record.args = _redact_arg(record.args)
        return record

    factory._nexus_secret_redaction = True
    logging.setLogRecordFactory(factory)
    return True
