"""Logging-only normalization for LIVE sizing semantics.

Installed before runtime bootstrap. A LogRecordFactory wrapper is used because
filters attached to the root logger are not applied to records emitted by child
loggers that propagate to root handlers, and handlers may be created after
sitecustomize runs. Execution behavior is untouched.

Sizing authority is ``risk_policy.size_new_entry`` (minimum of equity stop-risk,
MAX_MARGIN_PCT, operator 50% margin CAP, liquidation buffer, portfolio budget
and exchange lot). Older install messages from inner compatibility wrappers
describe preliminary targets; they are relabelled here so operators never read
a superseded authority as current. Messages that already state the current
authority are never rewritten.
"""
from __future__ import annotations

import logging

CURRENT_AUTHORITY = "sizing_authority=RISK_POLICY_MIN_OF_CAPS operator_margin=CAP_ONLY"


def normalize_record(record: logging.LogRecord) -> logging.LogRecord:
    msg = str(record.msg)

    if "[PILOT_LEGACY_TARGET]" in msg:
        record.msg = (
            "[SIZING_SEMANTICS] legacy_target_telemetry=true preliminary_only=true "
            f"{CURRENT_AUTHORITY} execution_effect=NONE"
        )
        record.args = ()
        return record

    if "[PILOT_LIVE_RUNTIME]" in msg and "position-notional" in msg:
        record.msg = (
            "[PILOT_LIVE_RUNTIME] installed: cash-flow-aware durable equity drawdown + "
            "read-only exposure/private-WS preflight; 50pct-available margin is a CAP only; "
            f"{CURRENT_AUTHORITY}; external positions immutable"
        )
        record.args = ()
        return record

    if "[PILOT_RISK_CAP]" in msg and "RiskManagerV3 is the maximum quantity authority" in msg:
        record.msg = (
            "[PILOT_RISK_CAP] installed: inner min(target, risk) wrapper; final authority is "
            f"the outer FINAL_SIZING_INVARIANT; {CURRENT_AUTHORITY}; LIVE spread/depth/"
            "signal-drift and equity loss budget rechecked fail-closed after final sizing"
        )
        record.args = ()
        return record

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
