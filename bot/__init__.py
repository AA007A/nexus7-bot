"""NEXUS-7 package bootstrap."""

# Install credential redaction before importing any module that may emit logs.
# This changes log rendering only; authentication and trading behavior are untouched.
from bot import log_redaction_hardening as _log_redaction_hardening

_log_redaction_hardening.install()
