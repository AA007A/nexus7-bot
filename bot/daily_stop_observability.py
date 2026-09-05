"""Observability hardening for the intentionally disabled daily stop.

The DailyTracker does not enforce a daily stop, but legacy engine telemetry can
still print a configured DAILY_STOP_LOSS value. This module only corrects that
presentation; it does not change risk, thresholds, execution, or trading mode.
"""
import logging
import re


class _DailyStopLabelFilter(logging.Filter):
    """Rewrite legacy daily-stop telemetry so it matches effective behavior."""

    _legacy = re.compile(r"\s*\|\s*Stop-loss dia:\s*-\$[-+\d.,]+")

    def filter(self, record):
        try:
            msg = record.getMessage()
            if "Stop-loss dia:" in msg:
                msg = self._legacy.sub(" | Stop diário: DESATIVADO", msg)
                record.msg = msg
                record.args = ()
            elif "stop_diário=" in msg:
                # daily_stopped can still become True for weekly/monthly stops;
                # do not label that state as an active daily loss limit.
                msg = msg.replace(
                    "stop_diário=", "bloqueio_periodico="
                )
                record.msg = msg
                record.args = ()
        except Exception as exc:
            record.msg = (
                f"[DAILY_STOP_OBSERVABILITY_ERROR] {type(exc).__name__}: {exc}"
            )
            record.args = ()
        return True


def install(log):
    """Install presentation-only corrections on stream handlers."""
    if getattr(log, "_daily_stop_observability_patched", False):
        return

    for handler in getattr(log, "handlers", []):
        if isinstance(handler, logging.StreamHandler):
            handler.addFilter(_DailyStopLabelFilter())

    log._daily_stop_observability_patched = True
    log.info(
        "[DAILY_STOP] daily loss stop is disabled; weekly/monthly protections remain observable"
    )
