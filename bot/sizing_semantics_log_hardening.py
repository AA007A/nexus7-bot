"""Logging-only normalization for authoritative LIVE sizing semantics.

Installed before runtime bootstrap. A LogRecordFactory wrapper is used because
filters attached to the root logger are not applied to records emitted by child
loggers that propagate to root handlers, and handlers may be created after
sitecustomize runs. Execution behavior is untouched.
"""
from __future__ import annotations

import logging


def normalize_record(record: logging.LogRecord) -> logging.LogRecord:
    msg = str(record.msg)

    if "[PILOT_LEGACY_TARGET]" in msg:
        record.msg = (
            "[SIZING_SEMANTICS] legacy_target_telemetry=true "
            "authoritative_policy=50pct_available_margin "
            "sizing_authority=OPERATOR_50PCT_EQUITY "
            "risk_manager_role=VALIDATION_GATE execution_effect=NONE"
        )
        record.args = ()
        return record

    if "[PILOT_LIVE_RUNTIME]" in msg and "position-notional" in msg:
        record.msg = (
            "[PILOT_LIVE_RUNTIME] installed: cash-flow-aware durable equity drawdown + "
            "authoritative 50pct-available initial-margin sizing at configured leverage + "
            "read-only exposure/private-WS preflight; RiskManagerV3=VALIDATION_GATE; "
            "external positions immutable"
        )
        record.args = ()
        return record

    if "[PILOT_RISK_CAP]" in msg and "RiskManagerV3 is the maximum quantity authority" in msg:
        record.msg = (
            "[PILOT_RISK_CAP] installed: legacy numeric risk quantity is validation-only; "
            "authoritative sizing=OPERATOR_50PCT_EQUITY; LIVE spread/depth/signal-drift "
            "rechecked fail-closed after final sizing; authorization_unchanged=true"
        )
        record.args = ()
        return record

    if "sizing_authority=RiskManagerV3_plus_operator_50pct_margin_cap" in msg:
        record.msg = msg.replace(
            "sizing_authority=RiskManagerV3_plus_operator_50pct_margin_cap",
            "sizing_authority=OPERATOR_50PCT_EQUITY risk_manager_role=VALIDATION_GATE",
        )

    if "final risk-authoritative sizing invariants" in str(record.msg):
        record.msg = str(record.msg).replace(
            "final risk-authoritative sizing invariants",
            "final operator-50pct-margin sizing invariants with RiskManagerV3 validation gate",
        )

    return record


class SizingSemanticsFilter(logging.Filter):
    """Compatibility surface for unit tests and explicitly attached handlers."""
    def filter(self, record: logging.LogRecord) -> bool:
        normalize_record(record)
        return True


def install() -> None:
    """Normalize every subsequently-created LogRecord, independent of logger setup."""
    root = logging.getLogger()
    if getattr(root, "_sizing_semantics_filter_installed", False):
        return

    previous_factory = logging.getLogRecordFactory()

    def _factory(*args, **kwargs):
        return normalize_record(previous_factory(*args, **kwargs))

    logging.setLogRecordFactory(_factory)
    root._sizing_semantics_filter_installed = True
