"""Observability for the configured daily loss stop."""


def install(log):
    """Report the effective configured daily stop without changing behavior."""
    if getattr(log, "_daily_stop_observability_patched", False):
        return

    from bot.config import cfg

    log._daily_stop_observability_patched = True
    log.info(
        f"[DAILY_STOP] enabled: {cfg.DAILY_STOP_LOSS_PCT * 100:.1f}% of balance "
        "(or DAILY_STOP_LOSS absolute override when > 0)"
    )
