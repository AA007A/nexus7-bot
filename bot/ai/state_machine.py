"""Autonomous operation state machine. Every transition is observable
(appended to ``transitions``); an illegal transition or any ambiguous
execution state moves to HALT/RECOVER — never silently continues.

Order idempotency: an intent is keyed by its deterministic client_oid. A
timeout / lost ACK is AMBIGUOUS: the machine reconciles against the exchange
(by client_oid) BEFORE any resubmission; a timeout is never treated as a failure.
"""
from __future__ import annotations

from dataclasses import dataclass, field

STATES = ("BOOT", "SYNC_EXCHANGE", "WAIT_FOR_DATA", "BUILD_FEATURES", "AI_DECISION", "VALIDATE",
          "RISK_CHECK", "PLACE_ORDER", "CONFIRM_ORDER", "INSTALL_PROTECTION", "MANAGE_POSITION",
          "RECONCILE", "CLOSE", "JOURNAL", "RECOVER", "HALT")

ALLOWED = {
    "BOOT": {"SYNC_EXCHANGE", "HALT"},
    "SYNC_EXCHANGE": {"WAIT_FOR_DATA", "MANAGE_POSITION", "RECOVER", "HALT"},
    "WAIT_FOR_DATA": {"BUILD_FEATURES", "WAIT_FOR_DATA", "HALT"},
    "BUILD_FEATURES": {"AI_DECISION", "WAIT_FOR_DATA", "HALT"},
    "AI_DECISION": {"VALIDATE", "JOURNAL", "HALT"},
    "VALIDATE": {"RISK_CHECK", "JOURNAL", "HALT"},
    "RISK_CHECK": {"PLACE_ORDER", "JOURNAL", "HALT"},
    "PLACE_ORDER": {"CONFIRM_ORDER", "RECONCILE", "HALT"},
    "CONFIRM_ORDER": {"INSTALL_PROTECTION", "RECONCILE", "JOURNAL", "HALT"},
    "INSTALL_PROTECTION": {"MANAGE_POSITION", "RECOVER", "HALT"},
    "MANAGE_POSITION": {"CLOSE", "RECONCILE", "MANAGE_POSITION", "HALT"},
    "RECONCILE": {"CONFIRM_ORDER", "MANAGE_POSITION", "WAIT_FOR_DATA", "RECOVER", "HALT"},
    "CLOSE": {"JOURNAL", "RECONCILE", "HALT"},
    "JOURNAL": {"WAIT_FOR_DATA", "HALT"},
    "RECOVER": {"SYNC_EXCHANGE", "HALT"},
    "HALT": {"RECOVER"},
}


class IllegalTransition(RuntimeError):
    pass


@dataclass
class AutonomousStateMachine:
    state: str = "BOOT"
    transitions: list = field(default_factory=list)
    intents: dict = field(default_factory=dict)       # client_oid -> status

    def go(self, new: str, *, reason: str = "") -> str:
        if new not in ALLOWED.get(self.state, set()):
            self.transitions.append({"from": self.state, "to": "HALT",
                                     "reason": f"ILLEGAL_TRANSITION:{self.state}->{new}"})
            self.state = "HALT"
            raise IllegalTransition(f"{self.transitions[-1]['reason']}")
        self.transitions.append({"from": self.state, "to": new, "reason": reason})
        self.state = new
        return new

    # ── order submission with reconcile-before-retry ─────────────────────
    def submit(self, intent, send, lookup) -> str:
        """``send(intent)`` may return 'ACK', 'REJECTED' or raise TimeoutError.
        ``lookup(client_oid)`` queries the exchange: 'FOUND', 'NOT_FOUND' or
        raises (unknown). Never resubmits without a definitive NOT_FOUND."""
        oid = intent.client_oid
        prior = self.intents.get(oid)
        if prior in ("ACK", "FOUND", "PENDING_UNKNOWN"):
            return self._reconcile(oid, lookup) if prior == "PENDING_UNKNOWN" else prior
        self.go("PLACE_ORDER", reason=oid)
        try:
            res = send(intent)
        except TimeoutError:
            self.intents[oid] = "PENDING_UNKNOWN"
            self.go("RECONCILE", reason="ACK_LOST")
            return self._reconcile(oid, lookup)
        self.intents[oid] = res
        if res == "ACK":
            self.go("CONFIRM_ORDER", reason=oid)
        else:
            self.go("RECONCILE", reason=f"SEND_{res}")
        return res

    def _reconcile(self, oid, lookup) -> str:
        try:
            found = lookup(oid)
        except Exception:
            self.intents[oid] = "PENDING_UNKNOWN"
            if self.state != "HALT":
                self.go("HALT", reason="RECONCILIATION_UNCERTAIN")
            return "PENDING_UNKNOWN"
        self.intents[oid] = "FOUND" if found == "FOUND" else "NOT_FOUND"
        if found == "FOUND" and self.state == "RECONCILE":
            self.go("CONFIRM_ORDER", reason="RECONCILED_FOUND")
        return self.intents[oid]

    # ── protection invariant ─────────────────────────────────────────────
    def protect(self, install, confirm, fail_safe) -> bool:
        """After a confirmed entry: install SL/TP and confirm. If protection
        cannot be confirmed, run the predeclared fail-safe and HALT."""
        self.go("INSTALL_PROTECTION")
        ok = False
        try:
            install()
            ok = bool(confirm())
        except Exception:
            ok = False
        if ok:
            self.go("MANAGE_POSITION", reason="PROTECTED")
            return True
        fail_safe()
        self.go("HALT", reason="UNPROTECTED_POSITION")
        return False
