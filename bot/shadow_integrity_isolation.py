"""SHADOW-only integrity isolation for manual/external positions.

Allows hypothetical scanning to continue when the *only* blocking integrity
issues are an external/manual position divergence and/or missing confirmed stop
on that external position. This never grants exchange execution permission.
The validation safety lock must already be active and non-PAPER.
"""

import time
import types

_ALLOWED_SHADOW_ONLY_CODES = {"STATE_DIVERGENCE", "POSITION_WITHOUT_STOP"}


def install_for_engine(engine, log):
    guard = getattr(engine, "integrity", None)
    if guard is None or getattr(guard, "_shadow_integrity_isolation_installed", False):
        return

    original_can_open = guard.can_open_new

    def _shadow_can_open_new(self_guard):
        # Outside validation-held SHADOW, preserve the original fail-closed rule.
        if getattr(engine, "paper_trade", False):
            return original_can_open()
        if not getattr(engine, "_validation_safety_lock_active", False):
            return original_can_open()

        # If the normal guard already permits analysis, do not alter anything.
        if original_can_open():
            return True

        state = getattr(self_guard, "state", None)
        if state is None or getattr(state, "checked_at", 0) <= 0:
            return False

        # Preserve freshness fail-closed behavior exactly.
        try:
            import os
            max_age = float(os.environ.get("INTEGRITY_MAX_AGE", "300"))
            if time.time() - float(state.checked_at) > max_age:
                return False
        except Exception:
            return False

        blocked = [
            issue for issue in getattr(state, "issues", [])
            if getattr(getattr(issue, "severity", None), "value", None) == "BLOCKED"
        ]
        if not blocked:
            return False

        blocked_codes = {getattr(issue, "code", "") for issue in blocked}
        hard_codes = blocked_codes - _ALLOWED_SHADOW_ONLY_CODES
        if hard_codes:
            return False

        # The execution path remains intercepted by validation_safety_lock._open.
        now = time.time()
        last = float(getattr(self_guard, "_shadow_integrity_last_log", 0.0) or 0.0)
        if now - last >= 60.0:
            self_guard._shadow_integrity_last_log = now
            log.warning(
                "[SHADOW_STATE_ISOLATION] external/manual integrity blockers "
                "ignored for hypothetical scan only; execution_effect=NONE; "
                "real execution remains fail-closed"
            )
        return True

    guard.can_open_new = types.MethodType(_shadow_can_open_new, guard)
    guard._shadow_integrity_isolation_installed = True
