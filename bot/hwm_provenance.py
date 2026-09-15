"""Durable, non-secret provenance for LIVE account-equity HWM transitions.

This module is accounting/observability only. It does not authorize execution,
change risk thresholds, leverage, sizing, strategy, orders, or exchange state.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from bot import database as db

HWM_PROVENANCE_KEY = "risk:account_equity_peak:provenance:v1"
_ALLOWED_REASONS = {
    "bootstrap",
    "new_equity_high",
    "incident_repair",
    "external_capital_flow_rebase",
}


def _finite_positive(value, label: str) -> float:
    value = float(value)
    if not (value > 0.0) or value == float("inf") or value != value:
        raise ValueError(f"{label} must be positive and finite")
    return value


async def persist_hwm_provenance(
    *,
    reason: str,
    old_peak: float | None,
    new_peak: float,
    account_equity: float,
    evidence_ref: str,
    strict: bool = True,
) -> bool:
    """Persist one compact provenance snapshot alongside an HWM transition."""
    if reason not in _ALLOWED_REASONS:
        raise ValueError("unsupported HWM provenance reason")
    new_peak = _finite_positive(new_peak, "new_peak")
    account_equity = _finite_positive(account_equity, "account_equity")
    old = None if old_peak is None else _finite_positive(old_peak, "old_peak")
    evidence_ref = str(evidence_ref or "").strip()
    if not evidence_ref or len(evidence_ref) > 160:
        raise ValueError("evidence_ref must be non-empty and <=160 chars")

    payload = {
        "version": 1,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "reason": reason,
        "old_peak": old,
        "new_peak": new_peak,
        "account_equity": account_equity,
        "evidence_ref": evidence_ref,
        "execution_effect": "NONE",
    }
    return bool(
        await db.save_key_value(
            HWM_PROVENANCE_KEY,
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
            strict=strict,
        )
    )
