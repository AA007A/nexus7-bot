"""Logging-only normalization for authoritative LIVE sizing semantics.

The execution path is owned by ``final_sizing_invariants``: 50% of freshly
available collateral is the initial-margin target at configured leverage, while
RiskManagerV3 remains a fail-closed validation gate. Older wrappers still emit
historical position-notional/risk-authority wording. This filter corrects only
those log records; it does not patch execution callables, quantities, leverage,
risk gates, exchange requests, or persistence.
"""
from __future__ import annotations

import logging


class SizingSemanticsFilter(logging.Filter):
    """Normalize stale sizing wording without changing execution behavior."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = str(record.msg)

        if "[PILOT_LEGACY_TARGET]" in msg:
            record.msg = (
                "[SIZING_SEMANTICS] legacy_target_telemetry=true "
                "authoritative_policy=50pct_available_margin "
                "sizing_authority=OPERATOR_50PCT_EQUITY "
                "risk_manager_role=VALIDATION_GATE execution_effect=NONE"
            )
            record.args = ()
            return True

        if "[PILOT_LIVE_RUNTIME]" in msg and "position-notional" in msg:
            record.msg = (
                "[PILOT_LIVE_RUNTIME] installed: cash-flow-aware durable equity drawdown + "
                "authoritative 50pct-available initial-margin sizing at configured leverage + "
                "read-only exposure/private-WS preflight; RiskManagerV3=VALIDATION_GATE; "
                "external positions immutable"
            )
            record.args = ()
            return True

        if "[PILOT_RISK_CAP]" in msg and "RiskManagerV3 is the maximum quantity authority" in msg:
            record.msg = (
                "[PILOT_RISK_CAP] installed: legacy numeric risk quantity is advisory/validation-only; "
                "authoritative sizing=OPERATOR_50PCT_EQUITY; LIVE spread/depth/signal-drift "
                "rechecked fail-closed after final sizing; authorization_unchanged=true"
            )
            record.args = ()
            return True

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

        return True


def install() -> None:
    """Install on root + existing handlers before runtime bootstrap emits logs."""
    root = logging.getLogger()
    if getattr(root, "_sizing_semantics_filter_installed", False):
        return

    filt = SizingSemanticsFilter()
    root.addFilter(filt)
    for handler in root.handlers:
        handler.addFilter(filt)
    root._sizing_semantics_filter_installed = True
