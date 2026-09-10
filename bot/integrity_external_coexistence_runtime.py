"""Runtime integration for protected external-position coexistence.

This installer changes only IntegrityGuard classification after the normal
read-only integrity assessment has completed. It never creates, modifies,
cancels, adopts, reduces, protects, or closes any exchange order/position.

A protected external/manual position may be DEGRADED instead of BLOCKED only
when the explicit pilot exposure-capacity policy is installed and enabled. Any
unprotected external position, any other BLOCKED issue, stale integrity state,
or missing policy remains fail-closed.
"""

from bot.integrity import IntegrityIssue, Severity
from bot.integrity_protected_external_policy import protected_external_coexistence_enabled


def install(IntegrityGuard, log):
    if getattr(IntegrityGuard, "_protected_external_coexistence_runtime_patched", False):
        return

    original_assess = IntegrityGuard.assess

    async def _assess_with_protected_external_coexistence(self, client, engine):
        state = await original_assess(self, client, engine)

        if not protected_external_coexistence_enabled(engine):
            return state

        changed = False
        rewritten = []
        for issue in state.issues:
            if issue.code == "EXTERNAL_POSITION_PROTECTED" and issue.severity == Severity.BLOCKED:
                rewritten.append(
                    IntegrityIssue(
                        code=issue.code,
                        severity=Severity.DEGRADED,
                        detail=(
                            issue.detail
                            + "; coexistência permitida sob PilotGuard com capacidade/exposição explícita"
                        ),
                        ts=issue.ts,
                    )
                )
                changed = True
            else:
                rewritten.append(issue)

        if not changed:
            return state

        state.issues = rewritten
        if any(i.severity == Severity.BLOCKED for i in rewritten):
            state.severity = Severity.BLOCKED
        elif any(i.severity == Severity.DEGRADED for i in rewritten):
            state.severity = Severity.DEGRADED
        else:
            state.severity = Severity.OK

        self.state = state
        log.info(
            "[PROTECTED_EXTERNAL_COEXISTENCE] enabled=true policy=pilot_exposure_capacity "
            "manual_position=read_only execution_effect=NONE"
        )
        return state

    IntegrityGuard.assess = _assess_with_protected_external_coexistence
    IntegrityGuard._protected_external_coexistence_runtime_patched = True
    log.info(
        "[PROTECTED_EXTERNAL_COEXISTENCE] installed: protected external positions may "
        "coexist only under explicit pilot capacity policy; execution_effect=NONE"
    )
