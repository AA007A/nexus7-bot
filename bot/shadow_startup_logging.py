"""Startup-only observability correction for validation-held SHADOW LIVE.

This module changes log wording only. It does not alter trading mode, release
gates, exchange credentials, order routing, sizing, or risk decisions.
"""

import logging
import os

_INSTALLED = False
_ORIGINAL_FACTORY = None


def install_preimport():
    global _INSTALLED, _ORIGINAL_FACTORY
    if _INSTALLED:
        return

    # Only relevant when the process is configured non-PAPER. The independent
    # validation safety lock remains the authority that blocks mutations.
    if os.environ.get("PAPER_TRADE", "").strip().lower() != "false":
        _INSTALLED = True
        return

    _ORIGINAL_FACTORY = logging.getLogRecordFactory()

    def _factory(*args, **kwargs):
        record = _ORIGINAL_FACTORY(*args, **kwargs)
        msg = record.getMessage()
        if "OPERAÇÃO REAL ATIVA" in msg:
            record.msg = (
                "🟣 SHADOW LIVE ATIVO — leitura/análise real; "
                "mutações de exchange bloqueadas pelo VALIDATION_LOCK"
            )
            record.args = ()
            record.levelno = logging.WARNING
            record.levelname = "WARNING"
        elif record.levelno >= logging.CRITICAL and msg.strip() and set(msg.strip()) == {"="}:
            # The production startup banner surrounds the LIVE-mode message with
            # CRITICAL-level separator lines. Under validation-held SHADOW these
            # separators are presentation only, not incidents. Demote only pure
            # separator records; genuine CRITICAL messages remain untouched.
            record.levelno = logging.WARNING
            record.levelname = "WARNING"
        return record

    logging.setLogRecordFactory(_factory)
    _INSTALLED = True
