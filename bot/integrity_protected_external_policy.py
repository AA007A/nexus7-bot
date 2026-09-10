"""Policy helper for coexistence with protected external/manual positions.

This module is pure/read-only: it never creates, modifies, cancels, adopts, or
closes exchange positions/orders. It only answers whether IntegrityGuard may
classify a protected external position as DEGRADED instead of BLOCKED.

The relaxation is allowed only when the real-pilot object is enabled AND the
explicit pilot exposure-capacity policy is installed on that PilotGuard class.
That policy counts external positions toward pilot concurrency and applies
fresh capital capacity checks. In every other case the caller must remain
fail-closed.
"""


def protected_external_coexistence_enabled(engine) -> bool:
    pilot = getattr(engine, "pilot", None)
    if pilot is None or not getattr(pilot, "enabled", False):
        return False
    return bool(getattr(type(pilot), "_exposure_capacity_patched", False))
