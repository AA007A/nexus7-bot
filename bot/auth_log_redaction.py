"""Central redaction for authentication-sensitive operational logs.

This filter is observability-only. It never changes credentials, request headers,
signatures, exchange calls, trading state, sizing, leverage, or authorization.
It replaces diagnostic messages that expose partial identifiers, credential
lengths, signing material, or authentication header values with non-sensitive
status text before handlers persist or forward the record.
"""
from __future__ import annotations

import logging
import re

_INSTALLED_ATTR = "_nexus_auth_log_redaction_installed"

_SENSITIVE_HEADER_RE = re.compile(
    r"(?i)(KC-API-(?:KEY|SIGN|PASSPHRASE)|Authorization)\s*[:=]\s*[^\s,;]+"
)


class _AuthRedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            rendered = record.getMessage()
            replacement = None

            if "KuCoin API Key:" in rendered:
                replacement = "[AUTH_DIAGNOSTIC] KuCoin API key configured=true"
            elif "API Secret:" in rendered and "Passphrase:" in rendered:
                replacement = "[AUTH_DIAGNOSTIC] KuCoin secret/passphrase configured=true"
            elif rendered.startswith("🔏 Assinando:") or "sign[:12]=" in rendered:
                replacement = "[AUTH_DIAGNOSTIC] KuCoin request signature generated"
            elif "Passphrase contém chars especiais:" in rendered:
                replacement = (
                    "[AUTH_DIAGNOSTIC] KuCoin passphrase contains special characters; "
                    "value_not_logged=true"
                )
            elif _SENSITIVE_HEADER_RE.search(rendered):
                replacement = _SENSITIVE_HEADER_RE.sub(r"\1=[REDACTED]", rendered)

            if replacement is not None:
                record.msg = replacement
                record.args = ()
        except Exception:
            # Logging hardening must never affect trading/risk execution.
            return True
        return True


def install(log) -> None:
    """Attach redaction once to the application logger and all current handlers."""
    if getattr(log, _INSTALLED_ATTR, False):
        return
    filt = _AuthRedactionFilter()
    log.addFilter(filt)
    for handler in list(getattr(log, "handlers", ()) or ()):
        handler.addFilter(filt)
    setattr(log, _INSTALLED_ATTR, True)
    log.info(
        "[AUTH_LOG_REDACTION] installed=true credential_values=false "
        "credential_lengths=false signature_material=false execution_effect=NONE"
    )
