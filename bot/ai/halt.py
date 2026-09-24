"""Automatic halt of NEW entries. The AI can raise a halt but never clear it.

Halt conditions (any one): risk drawdown, daily stop, stale market data,
model artifact mismatch, feature schema mismatch, DB reconciliation
uncertainty, exchange position mismatch, unprotected position, clock anomaly,
Stage-C LIVE evidence invalid/stale. Only an OPERATOR clear (with a reason)
re-enables entries; the clear is journaled.
"""
from __future__ import annotations

from dataclasses import dataclass, field

HALT_CONDITIONS = frozenset({
    "RISK_DRAWDOWN", "DAILY_STOP", "STALE_MARKET_DATA", "MODEL_ARTIFACT_MISMATCH",
    "FEATURE_SCHEMA_MISMATCH", "DB_RECONCILIATION_UNCERTAIN", "EXCHANGE_POSITION_MISMATCH",
    "UNPROTECTED_POSITION", "CLOCK_ANOMALY", "STAGE_C_EVIDENCE_INVALID",
    "RECONCILIATION_UNCERTAIN", "ILLEGAL_STATE_TRANSITION",
})
OPERATOR = "OPERATOR"


class HaltClearRefused(PermissionError):
    pass


@dataclass
class HaltController:
    active: set = field(default_factory=set)
    log: list = field(default_factory=list)

    @property
    def halted(self) -> bool:
        return bool(self.active)

    def raise_halt(self, condition: str, *, source: str, detail: str = "") -> None:
        if condition not in HALT_CONDITIONS:
            condition = "UNKNOWN_HALT:" + str(condition)
        self.active.add(condition)
        self.log.append({"event": "HALT", "condition": condition, "source": source, "detail": detail})

    def clear(self, condition: str, *, actor: str, reason: str) -> None:
        if actor != OPERATOR or not str(reason).strip():
            self.log.append({"event": "CLEAR_REFUSED", "condition": condition, "actor": actor})
            raise HaltClearRefused("only an operator with a stated reason may clear a halt")
        self.active.discard(condition)
        self.log.append({"event": "CLEAR", "condition": condition, "actor": actor, "reason": reason})
