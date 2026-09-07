"""Correct runtime mode observability while validation lock holds LIVE.

Observability only: no exchange calls, trading configuration, release gates,
or execution behavior are changed.
"""


def install(log):
    try:
        import main as app_main
    except Exception as exc:
        log.warning("[SHADOW_MODE] main observability patch unavailable: %s", type(exc).__name__)
        return

    original_health = getattr(app_main, "health", None)
    if original_health is None or getattr(app_main, "_shadow_mode_observability_patched", False):
        return

    async def health_shadow_aware():
        out = await original_health()
        if not isinstance(out, dict):
            return out
        engine = getattr(app_main.app.state, "engine", None)
        held = bool(
            engine is not None
            and not getattr(engine, "paper_trade", True)
            and getattr(engine, "_validation_safety_lock_active", False)
        )
        if held:
            out = dict(out)
            out["trading_mode"] = "SHADOW_LIVE"
            out["execution_effect"] = "NONE"
            out["orders_sent_to_exchange"] = False
            out["validation_lock"] = True
        return out

    app_main.health = health_shadow_aware
    # FastAPI routes keep the original endpoint callable; replace it too.
    for route in getattr(app_main.app, "routes", []):
        if getattr(route, "path", None) == "/health":
            route.endpoint = health_shadow_aware

    app_main._shadow_mode_observability_patched = True
    log.warning(
        "[SHADOW_MODE] observability installed: validation-held LIVE reports "
        "SHADOW_LIVE execution_effect=NONE"
    )
